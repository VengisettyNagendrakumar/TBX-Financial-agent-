"""
Finance Agent (Phase 4)
=======================
A tool-calling agent over the Phase 2 query layer.

Design decisions worth stating, because they are what make the behaviour
testable rather than emergent:

1. THE MODEL NEVER WRITES SQL. It selects a tool and fills typed arguments.
   Every argument is validated before execution and `entity_id` is injected
   server-side from the session, so the model can choose *what* to filter but
   never *whose data* to read.

2. CLARIFICATION IS A TOOL, NOT A PROMPT INSTRUCTION. `ask_user` suspends the
   turn. More importantly, POLICY GATES can force it: if the merchant is
   ambiguous, or a period is missing where it matters, the agent asks even if
   the model wanted to answer. Prompting a model to "ask when unsure" is a
   hope; a gate is a guarantee.

3. IT WORKS WITHOUT AN LLM. When no GROQ_API_KEY is present -- or the API
   fails -- a deterministic planner maps the question onto the same tools using
   the merchant vocabulary already in the warehouse. The app stays demonstrable
   and the failure mode is degraded phrasing, not a broken assistant.
"""

import os
import re
import json
import time
import uuid
from dataclasses import dataclass, field

import config
import db
import queries
import resolver
import explainer
import llm
import sqlguard
from db import RESOLVED, ALL_TIME, UNRESOLVED
from queries import UnresolvedFilterError

ANSWER = "ANSWER"
CLARIFY = "CLARIFY"
GUARDRAIL = "GUARDRAIL"
ERROR = "ERROR"


# Confidence bands. See ARCHITECTURE_V2.md §13 for the full methodology.
HIGH, MEDIUM, LOW = "High", "Medium", "Low"
BAND_HIGH_MIN = 0.88
BAND_MEDIUM_MIN = 0.72

# Per-signal factors, multiplied together. Multiplicative rather than "worst
# wins" so that several mild doubts compound into a real one -- a fuzzy-matched
# counterparty over an assumed period with patchy attribution should not read
# as confidently as any one of those alone.
PERIOD_ASSUMED_FACTOR = 0.92      # no period given; answered over all history
NARRATION_REJECTED_FACTOR = 0.85  # model wording failed the grounding check
# Answered with model-written SQL (sandboxed, see sqlguard.py). The arithmetic
# is still the database's, but the question -> query mapping was the model's
# alone, with no typed tool contract constraining it.
VIA_SQL_FACTOR = 0.85


def band_for(score: float) -> str:
    if score >= BAND_HIGH_MIN:
        return HIGH
    return MEDIUM if score >= BAND_MEDIUM_MIN else LOW


@dataclass
class Confidence:
    """
    How much to trust this answer.

    The arithmetic is always exact -- it comes from SQL, and a figure the
    database did not produce cannot reach the user. What varies is whether the
    QUESTION was interpreted correctly: the right counterparty, the right
    window, over data that is fully attributed.

    Only the band is shown. The underlying score is a means of combining
    signals, not a measurement anyone should read to two significant figures,
    and displaying "93%" invites exactly that. Every contributing signal is
    surfaced as a plain-language reason instead.
    """
    score: float = 1.0
    label: str = HIGH
    reasons: list = field(default_factory=list)

    @property
    def pct(self) -> int:
        """Internal only -- for logs and tests, never for display."""
        return int(round(self.score * 100))


@dataclass
class AgentResult:
    status: str
    answer: str = ""
    question: str = ""
    options: list = field(default_factory=list)
    result: object = None            # QueryResult
    kind: str = ""
    trace: list = field(default_factory=list)
    latency_ms: float = 0.0
    planner: str = ""                # 'llm' or 'rules'
    narration: str = ""              # 'llm' | 'llm_rejected' | 'template'
    pending: dict = field(default_factory=dict)
    resolution: object = None
    confidence: Confidence = field(default_factory=Confidence)
    inherited: dict = field(default_factory=dict)


# =============================================================
# TOOL SCHEMAS  (what the model may call)
# =============================================================

PERIOD_DESC = ("Time period. Use a canonical token: 'last_month', 'this_month', "
               "'two_months_ago', 'last_3_months', 'last_6_months', 'last_30_days', "
               "'last_quarter', 'q1'..'q4', 'ytd', 'last_year', a month name like "
               "'april', a calendar year like 'year_2026' for 'in 2026' / "
               "'the year 2026', or 'all_time' when the user says total/overall/ever. "
               "Omit entirely if the user gave no time period.")

TOOLS = [
    {"type": "function", "function": {
        "name": "get_spend",
        "description": "Total money out (debit) or in (credit), optionally for one "
                       "counterparty. Use for 'how much did I spend on X', "
                       "'how much have I paid X in total'.",
        "parameters": {"type": "object", "properties": {
            "merchant": {"type": "string", "description": "Counterparty name as the user said it, e.g. 'swiggy'."},
            "period": {"type": "string", "description": PERIOD_DESC},
            "direction": {"type": "string", "enum": ["debit", "credit"],
                          "description": "'debit' for money the user spent, 'credit' for money received."},
            "group_by_month": {"type": "boolean",
                               "description": "Set true for 'which month did I spend the "
                                              "most', 'monthly breakdown', 'spend per month'."},
        }, "required": ["direction"]}}},

    {"type": "function", "function": {
        "name": "rank_counterparties",
        "description": "Rank counterparties by amount. Use for 'which vendor have I "
                       "spent the most on', 'who paid me the most', 'top merchants'.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["debit", "credit"]},
            "period": {"type": "string", "description": PERIOD_DESC},
            "kind": {"type": "string", "enum": ["merchant", "person"],
                     "description": "Set 'person' when the user asks about individuals (a friend, someone who paid them)."},
            "limit": {"type": "integer", "description": "How many to show. Default 10."},
        }, "required": ["direction"]}}},

    {"type": "function", "function": {
        "name": "compare_spend",
        "description": "Compare two periods side by side with the difference. Use for "
                       "'how does that compare to last month', 'is that more than before'.",
        "parameters": {"type": "object", "properties": {
            "merchant": {"type": "string"},
            "period_b": {"type": "string",
                         "description": "The period the user is ASKING ABOUT. " + PERIOD_DESC},
            "period_a": {"type": "string",
                         "description": "The baseline to compare against, ONLY if the "
                                        "user named a specific one (e.g. 'versus "
                                        "April'). OMIT this for 'the period before', "
                                        "'the 3 months before that', 'the previous "
                                        "quarter' -- the system derives the "
                                        "equal-length window immediately before "
                                        "period_b. " + PERIOD_DESC},
            "direction": {"type": "string", "enum": ["debit", "credit"]},
        # period_a is intentionally NOT required. Marking it required made Groq
        # reject the model's (correct) omission with a schema-validation error,
        # silently dropping every comparison onto the rules planner.
        }, "required": ["period_b", "direction"]}}},

    {"type": "function", "function": {
        "name": "list_transactions",
        "description": "Individual transactions. Use for 'show me those', 'list my "
                       "payments to X'.",
        "parameters": {"type": "object", "properties": {
            "merchant": {"type": "string"},
            "period": {"type": "string", "description": PERIOD_DESC},
            "direction": {"type": "string", "enum": ["debit", "credit"]},
            "limit": {"type": "integer",
                      "description": "How many rows. 'last 5 transactions' -> 5; "
                                     "'highest/lowest transaction' -> 1."},
            "order_by": {"type": "string", "enum": ["date", "amount"],
                         "description": "'amount' for highest/lowest/largest/smallest "
                                        "single transaction; 'date' (default) for "
                                        "latest/recent."},
            "ascending": {"type": "boolean",
                          "description": "true for lowest/smallest/oldest; false (default) "
                                         "for highest/largest/latest."},
            "min_amount": {"type": "number",
                           "description": "Only transactions >= this amount, e.g. 'more "
                                          "than one lakh' -> 100000."},
            "max_amount": {"type": "number"},
        }, "required": []}}},

    {"type": "function", "function": {
        "name": "get_balances",
        "description": "Account balances and account details. Use for 'what is my "
                       "balance', 'how much do I have', 'show my other accounts', "
                       "'account details', 'list my accounts', 'which banks am I "
                       "with'. Prefer this over query_sql for anything about "
                       "accounts rather than transactions.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},

    {"type": "function", "function": {
        "name": "ask_user",
        "description": "Ask the user a clarifying question when the request is "
                       "genuinely ambiguous. Prefer this over guessing.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        }, "required": ["question"]}}},

    {"type": "function", "function": {
        "name": "query_sql",
        "description": "FALLBACK when no other tool fits: thresholds ('transactions "
                       "over 1 lakh'), counts, day-of-week or channel breakdowns, "
                       "ranges, or any combination the typed tools cannot express. "
                       "Write ONE SELECT over the two views in the schema. Never "
                       "use this when get_spend / list_transactions / "
                       "rank_counterparties / compare_spend / get_balances can "
                       "answer -- they are more reliable.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string",
                    "description": "One SELECT over my_transactions and/or my_accounts, "
                                   "with '?' placeholders for every user-supplied "
                                   "literal, and a LIMIT."},
            "params": {"type": "array", "items": {},
                       "description": "Values for the '?' placeholders, in order."},
            "purpose": {"type": "string",
                        "description": "One sentence: what this query answers."},
        }, "required": ["sql", "params", "purpose"]}}},
]

SYSTEM_PROMPT = """You convert personal-finance questions into tool calls.

The dataset's most recent transaction is {anchor}. Resolve all relative periods
against that date, not today's date.

Guidance:
- "spent on X" / "paid X" -> get_spend with direction='debit'
- "paid me" / "received from" / "sent me" -> direction='credit'
- "which vendor/merchant did I spend most on" -> rank_counterparties
- "what was my last/latest/most recent transaction" -> list_transactions with
  limit 5, no merchant and no period
- Comparisons -> compare_spend. Put the window the user is ASKING ABOUT in
  'period_b'. If the baseline is "the period before", "the 3 months before
  that", or similar, OMIT 'period_a' entirely -- the system derives the
  equal-length window immediately before 'period_b'. Never put a time phrase in
  'merchant'.
- "my friend paid me" (NO name given) -> rank_counterparties(direction='credit',
  kind='person'). But "Gautam Singh paid me" NAMES a person -> get_spend with
  merchant='Gautam Singh', direction='credit'. A named individual is a single
  total, never a ranking.
- "total" / "overall" / "ever" / "all time" -> period='all_time'
- If the user names no time period and is asking about one counterparty, still
  call the tool without a period; the system will ask if a period is needed.

Context rules:
- Carry the counterparty from earlier turns ONLY for a follow-up that refers
  back ("show me those", "what about April?").
- A question that widens the scope -- "in general", "overall", "my last
  transaction", "all my transactions" -- is a NEW question. Do not set
  'merchant' for it, even if the previous turn was about one.
- Leave 'direction' unset for a plain "what was my last transaction"; it may be
  money in or out.

Single-transaction questions:
- "highest / largest / biggest transaction or amount" -> list_transactions with
  order_by='amount', ascending=false, limit=1. "lowest / smallest" -> ascending=true.
  These are about ONE transaction, not a vendor ranking.
- "transactions over / more than / above X" -> list_transactions with min_amount.
  1 lakh = 100000, 1 crore = 10000000.
- "last N transactions (with X)" -> list_transactions, limit=N, order_by='date'.
  Do NOT ask for a period; "last N" is already bounded.
- "which month did I spend the most / monthly breakdown" -> get_spend with
  group_by_month=true.

Fallback:
- If, and only if, none of the tools above can express the question, use
  query_sql with the schema below. Prefer a typed tool whenever one fits.
- NEVER use query_sql for a question that names a merchant or a person. Use the
  typed tools so the name is resolved against the customer's real history and
  an unknown name is reported honestly, instead of returning an empty result
  that looks like "you spent nothing".

Call exactly one tool. Do not answer in prose."""


# =============================================================
# ARGUMENT NORMALISATION
# =============================================================

_DIRECTION_CREDIT = re.compile(
    r"\b(paid me|pay me|owe[sd]? me|sent me|send me|gave me|received|receive|"
    r"credited|refund(?:ed)?|income|salary|deposit(?:ed)?|got from|coming in)\b", re.I)
_DIRECTION_DEBIT = re.compile(
    r"\b(spen[dt]|spending|paid|pay|cost|charged|bought|purchase[sd]?|outgo)\b", re.I)

# References back to the previous answer: "show me THESE transactions".
_ANAPHORA = re.compile(
    r"\b(these|those|that|this|it|them|the same|above|breakdown of it)\b", re.I)

# Elliptical follow-ups: a fragment that only makes sense against the previous
# turn. "What about April?" carries no counterparty of its own.
_ELLIPTICAL = re.compile(
    r"^\s*(?:and\s+|but\s+|ok(?:ay)?[,\s]+|so\s+)?"
    r"(what|how)\s+about\b|^\s*(?:and|also|plus)\s+\w+|^\s*same\s+for\b", re.I)

# Markers that the user has deliberately WIDENED the scope. These must defeat
# inheritance -- "what was my last transaction in general" is not a question
# about the counterparty discussed a moment ago.
_GENERIC_SCOPE = re.compile(
    r"\b(in general|generally|overall|any\s+(?:transaction|vendor|merchant|payment)|"
    r"across\s+all|all\s+(?:my\s+)?(?:transactions?|vendors?|merchants?|payments?|"
    r"accounts?|spending)|anything|everything|regardless|as\s+a\s+whole|"
    r"entire\s+account|whole\s+account|overall\s+spending)\b", re.I)

# Replies to "which period?" that mean "no restriction". Anything here resolves
# to all-time instead of re-asking the same question.
_NO_PERIOD_REPLY = re.compile(
    r"^\s*(no|nope|none|n/a|na|any|all|everything|all time|all-time|whole|entire|"
    r"doesn'?t matter|does not matter|don'?t care|skip|no filter|no period|"
    r"not only\b.*|no only\b.*|no,?\s.*)\s*[.!]?\s*$", re.I)

# ...and replies that ALSO widen the counterparty: "not only swiggy", "all vendors".
_WIDEN_SCOPE_REPLY = re.compile(
    r"\b(not only|no only|all (?:vendors|merchants|counterparties|transactions)|"
    r"everything|everyone|any vendor|any merchant|across all)\b", re.I)

# "more than one lakh", "over ₹50,000", "above 2.5 crore", "greater than 10k".
_THRESHOLD = re.compile(
    r"\b(?:more than|greater than|higher than|over|above|exceed(?:s|ing)?|at least|"
    r"bigger than|larger than)\s+(?:₹|rs\.?|inr)?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?|one|two|three|four|five|ten|half a|a)\s*"
    r"(?P<unit>lakh|lakhs|lac|lacs|crore|crores|cr|k|thousand|million|mn)?\b", re.I)

_NUMBER_WORD_VALUES = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                       "five": 5, "ten": 10, "half a": 0.5}
_UNIT_MULTIPLIERS = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
                     "crore": 1e7, "crores": 1e7, "cr": 1e7,
                     "k": 1e3, "thousand": 1e3, "million": 1e6, "mn": 1e6}


def _parse_amount(num: str, unit: str = None) -> float:
    """'one' + 'lakh' -> 100000.0 ; '2.5' + 'crore' -> 25000000.0 ; '50,000' -> 50000.0"""
    n = (num or "").strip().lower()
    value = _NUMBER_WORD_VALUES.get(n)
    if value is None:
        value = float(n.replace(",", "") or 0)
    return float(value * _UNIT_MULTIPLIERS.get((unit or "").lower(), 1))


_ALL_TIME = re.compile(r"\b(in total|total|overall|all time|all-time|ever|lifetime|"
                       r"altogether|so far)\b", re.I)

_PERIOD_PATTERNS = [
    # A bare calendar year: "in 2026", "the year 2026", "during 2025", or a
    # reply of just "2026" to "which period?". Resolves to Jan 1 - Dec 31.
    (re.compile(r"\b(?:in|during|for|of|year|calendar year|fy)\s+((?:19|20)\d{2})\b", re.I),
     lambda m: f"year_{m.group(1)}"),
    (re.compile(r"^\s*((?:19|20)\d{2})\s*[.!?]?\s*$", re.I),
     lambda m: f"year_{m.group(1)}"),
    (re.compile(r"\blast\s+(\d{1,2})\s+months?\b", re.I), lambda m: f"last_{m.group(1)}_months"),
    (re.compile(r"\bpast\s+(\d{1,2})\s+months?\b", re.I), lambda m: f"last_{m.group(1)}_months"),
    (re.compile(r"\blast\s+(\d{1,4})\s+days?\b", re.I), lambda m: f"last_{m.group(1)}_days"),
    (re.compile(r"\blast\s+month\b", re.I), lambda m: "last_month"),
    (re.compile(r"\bprevious\s+month\b", re.I), lambda m: "last_month"),
    (re.compile(r"\bthis\s+month\b", re.I), lambda m: "this_month"),
    (re.compile(r"\bmonth\s+before\s+last\b", re.I), lambda m: "two_months_ago"),
    (re.compile(r"\bthe\s+month\s+before\b", re.I), lambda m: "two_months_ago"),
    (re.compile(r"\blast\s+quarter\b", re.I), lambda m: "last_quarter"),
    (re.compile(r"\bthis\s+quarter\b", re.I), lambda m: "this_quarter"),
    (re.compile(r"\b(q[1-4])\b", re.I), lambda m: m.group(1).lower()),
    (re.compile(r"\b(ytd|year to date|this year)\b", re.I), lambda m: "ytd"),
    (re.compile(r"\blast\s+year\b", re.I), lambda m: "last_year"),
    (re.compile(r"\bin\s+(january|february|march|april|may|june|july|august|"
                r"september|october|november|december)\b", re.I), lambda m: m.group(1).lower()),
]


def extract_period(text: str):
    """Finds a period phrase in free text. Returns a canonical token or None."""
    for pat, fn in _PERIOD_PATTERNS:
        m = pat.search(text or "")
        if m:
            return fn(m)
    if _ALL_TIME.search(text or ""):
        return "all_time"
    return None


def explicit_direction(text: str):
    """
    The direction the user actually stated, or None.

    Credit wins on a tie: 'how much did my friend pay me' contains 'pay'.
    """
    if _DIRECTION_CREDIT.search(text or ""):
        return config.TXN_CREDIT
    if _DIRECTION_DEBIT.search(text or ""):
        return config.TXN_DEBIT
    return None


def extract_direction(text: str, default: str = config.TXN_DEBIT):
    """
    Direction with a default.

    Spend questions default to debit, but a bare "what was my last
    transaction?" should not: defaulting there hides an incoming payment, so
    listing callers pass default=None to mean 'both'.
    """
    return explicit_direction(text) or default


# A phrase that describes time, not a counterparty. "compare it to the 3 months
# before" put "the 3 months before" through the resolver, which answered
# "I have no transactions for the three months before" -- a time expression
# reported as a missing vendor.
_PERIOD_WORDS = re.compile(
    r"\b(month|months|week|weeks|year|years|quarter|quarters|day|days|"
    r"period|periods|ago|earlier|before|previous|prior|preceding|last|past|"
    r"trailing|since|recent|yesterday|today|tomorrow|ytd|mtd|qtd|q[1-4]|"
    # Days and parts of the day: "on weekends" is when, not whom.
    r"weekend|weekends|weekday|weekdays|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|morning|mornings|afternoon|afternoons|evening|"
    r"evenings|night|nights|daily|weekly|monthly|yearly|annually)\b", re.I)

_MONTH_WORD = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
    re.I)


# Carry no meaning of their own in "the three months before that".
_PERIOD_FILLER = {"the", "a", "an", "that", "this", "it", "of", "in", "for",
                  "to", "those", "these", "my", "same", "and", "then"}
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight",
                 "nine", "ten", "eleven", "twelve", "couple", "few"}


def looks_like_period(text: str) -> bool:
    """
    True when a string is a time expression rather than a counterparty name.

    Checked before entity resolution: fuzzy-matching "the 3 months before"
    against a merchant list can only produce a wrong vendor or a misleading
    "I have no transactions for the three months before".

    Structural rather than a keyword count -- strip filler and numbers, then
    require EVERY remaining word to be temporal. "Last Mile Logistics" contains
    a time word but keeps 'mile' and 'logistics', so it stays a merchant.
    """
    if not text:
        return False
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", str(text))]
    core = [w for w in words
            if w not in _PERIOD_FILLER and w not in _NUMBER_WORDS and not w.isdigit()]
    if not core:
        return False  # only filler/numbers -- not enough to call it a period
    return all(_PERIOD_WORDS.fullmatch(w) or _MONTH_WORD.fullmatch(w) for w in core)


def extract_merchant(text: str, vocabulary: list):
    """
    Finds a known counterparty in the question.

    Matching against the warehouse vocabulary beats a generic noun-phrase regex:
    the names are already known, so the longest match wins and no parsing
    heuristic is required.
    """
    low = f" {(text or '').lower()} "
    best = None
    for v in vocabulary:
        name = v["name"]
        if name in (config.UNKNOWN_MERCHANT, "BANK CHARGES", "SELF TRANSFER"):
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", low):
            if best is None or len(name) > len(best):
                best = name
    if best:
        return best
    m = re.search(r"\b(?:on|to|for|with|at|from)\s+([A-Za-z][A-Za-z0-9&.'\- ]{2,30}?)"
                  r"(?=\s+(?:in|last|this|for|during|total|over|past|between)\b|[?.,!]|$)",
                  text or "", re.I)
    if m:
        cand = m.group(1).strip()
        if cand.lower() in {"me", "my", "it", "that", "them", "total", "all",
                            "the most", "most", "everything"}:
            return None
        # "compare it to the 3 months before" -- the object of "to" is a period.
        if looks_like_period(cand):
            return None
        # "on which month I have high expense" -- a clause, not a name. Merchant
        # names do not contain question words or first-person pronouns; a
        # candidate that does was produced by the preposition matching a
        # sentence fragment, and resolving it yields a misleading NOT_FOUND.
        if _CLAUSE_WORDS.search(cand):
            return None
        return cand
    return None


_CLAUSE_WORDS = re.compile(
    r"\b(which|what|how|when|where|who|whom|why|i|i've|i'd|my|me|have|has|had|"
    r"did|do|does|is|are|was|were|spent|spend|expense|expenses|spending)\b", re.I)


# =============================================================
# AGENT
# =============================================================

class FinanceAgent:
    def __init__(self, con, entity_id=None, session=None, checkpointer=None):
        # Accepts a Session (preferred) or a bare entity_id for tests.
        if session is None:
            import session as session_mod
            session = session_mod.load(con, entity_id=entity_id)
        self.session = session
        self.con = con
        self.entity_id = session.entity_id
        self.anchor = db.get_anchor_date(con)
        self.vocabulary = resolver.load_vocabulary(con, self.entity_id)
        self._people = [v["name"] for v in self.vocabulary
                        if v["kind"] == config.KIND_PERSON]

        # Per-turn side-channel for rich objects (QueryResult, Resolution,
        # TimeRange, Confidence), keyed by run_id. They are kept OUT of the
        # LangGraph state so checkpoints stay plain msgpack: pickling a
        # DataFrame-bearing class broke the app the moment Streamlit
        # hot-reloaded queries.py ("not the same object as queries.QueryResult").
        self._scratch = {}

        # Compiled once; the topology is static, only the state varies.
        from graph import build_graph
        self._graph = build_graph(self, checkpointer=checkpointer)

    # ---------- planning ----------

    def _plan_llm(self, message: str, history: list):
        if not llm.is_configured():
            return None
        try:
            # The schema for the SQL fallback travels in the system prompt.
            # It is column names and enums only -- never rows -- so the model
            # can write a query without ever having seen data.
            system = (SYSTEM_PROMPT.format(anchor=self.anchor) + "\n\n"
                      + sqlguard.SCHEMA_DOC.format(anchor=self.anchor))
            msgs = [{"role": "system", "content": system}]
            for h in history[-4:]:
                if h.get("role") in ("user", "assistant") and h.get("content"):
                    msgs.append({"role": h["role"], "content": str(h["content"])[:500]})
            msgs.append({"role": "user", "content": message})

            resp = llm.chat(msgs, tools=TOOLS, tool_choice="auto",
                            temperature=0.0, max_tokens=400,
                            reasoning_effort="low")
            call = llm.first_tool_call(resp)
            if call is None:
                return None
            name, raw_args = call
            return name, json.loads(raw_args or "{}")
        except Exception as e:
            print(f"[agent] LLM planning unavailable, using rules: {e}")
            return None

    def _plan_rules(self, message: str, history: list):
        """Deterministic planner. Keeps the app usable with no API key."""
        text = message or ""
        low = text.lower()
        direction = extract_direction(text)
        period = extract_period(text)
        merchant = extract_merchant(text, self.vocabulary)

        # Checked before the generic "show me …" branch, which would otherwise
        # swallow "show me all my accounts" as a transaction listing.
        if re.search(r"\b(balances?|how much do i have|in my account|savings|current|checking)\b"
                     r"|\b(?:all\s+|other\s+)?(?:my\s+)?(?:other\s+)?accounts?\b"
                     r"|\baccounts?\s+(?:details?|info(?:rmation)?|summary|list)\b", low):
            return "get_balances", {}

        if re.search(r"\b(compare|versus|vs\.?|difference between)\b", low) or \
           re.search(r"\bhow (?:does|did) (?:that|this|it) compare\b", low):
            prev = self._last_context(history)
            # period_a is left unset on purpose: the baseline is derived as the
            # equal-length window immediately before period_b. Hardcoding it
            # (it used to be "two_months_ago") compared a 3-month subject
            # against a single month.
            return "compare_spend", {
                "merchant": merchant or prev.get("merchant"),
                "period_b": period or prev.get("period_token") or "last_month",
                "direction": direction,
            }

        # "Which month did I spend the most" is a monthly breakdown, not a
        # vendor ranking -- checked before the rank branch, which would
        # otherwise claim "most".
        if re.search(r"\bwhich month\b|\bmonthly\b|\bby month\b|\bper month\b|"
                     r"\bmonth(?:s)?\b.{0,20}\b(high(?:est)?|most|least|lowest)\b", low):
            return "get_spend", {"merchant": merchant, "period": period,
                                 "direction": direction, "group_by_month": True}

        # "Transactions over one lakh": a threshold listing. Parsed first so the
        # amount is not mistaken for a limit.
        thr = _THRESHOLD.search(text)
        if thr:
            return "list_transactions", {
                "merchant": merchant, "period": period, "direction": direction,
                "min_amount": _parse_amount(thr.group("num"), thr.group("unit")),
                "order_by": "amount", "limit": 50}

        # "Highest amount I have done in a transaction" is ONE transaction. The
        # vendor ranking branch below must not claim it: it says "vendor" or
        # "who", this says "transaction" / "amount" / "payment".
        # No noun is required: "which is the lowest?" as a follow-up to
        # "highest transaction" has none. What routes it AWAY from here is a
        # vendor/person word ("which vendor did I spend most on" -> ranking).
        ext = re.search(r"\b(highest|largest|biggest|max(?:imum)?|lowest|smallest|"
                        r"min(?:imum)?|cheapest|most expensive)\b", low)
        if ext and not re.search(r"\b(vendor|merchant|counterpart(?:y|ies)|who|whom|"
                                 r"person|people|friend|month|months|week|year)\b", low):
            asc = bool(re.search(r"\b(lowest|smallest|min(?:imum)?|cheapest)\b", low))
            return "list_transactions", {
                "merchant": merchant, "period": period, "direction": direction,
                "order_by": "amount", "ascending": asc, "limit": 1}

        if re.search(r"\b(most|highest|top|largest|biggest|ranked?|who did i)\b", low):
            kind = None
            if re.search(r"\b(friend|person|people|someone|who)\b", low):
                kind = config.KIND_PERSON
            return "rank_counterparties", {
                "direction": direction, "period": period, "kind": kind, "limit": 10}

        if re.search(r"\b(friend|friends)\b", low):
            return "rank_counterparties", {
                "direction": config.TXN_CREDIT, "period": period,
                "kind": config.KIND_PERSON, "limit": 10}

        # "Last 5 transactions with BookMyShow" / "latest transaction": the most
        # recent N rows. An explicit count wins; otherwise a small default.
        # "first transaction in 2026" / "earliest 3 transactions" are the SAME
        # shape with the order flipped: oldest first.
        recent = re.search(r"\b(last|latest|most recent|recent|first|earliest|oldest)"
                           r"\s+(\d{1,3})?\s*(?:\w+\s+)?transactions?\b", low)
        if recent:
            n = int(recent.group(2)) if recent.group(2) else (
                1 if recent.group(1) in ("first", "earliest", "oldest") else 5)
            return "list_transactions", {
                "merchant": merchant, "period": period, "limit": n, "order_by": "date",
                "ascending": recent.group(1) in ("first", "earliest", "oldest")}

        if re.search(r"\b(list|show me|show all|transactions|breakdown|itemi[sz]e)\b", low):
            return "list_transactions", {
                "merchant": merchant, "period": period,
                "direction": direction, "limit": 50}

        if not merchant:
            prev = self._last_context(history)
            merchant = prev.get("merchant")
        return "get_spend", {"merchant": merchant, "period": period, "direction": direction}

    def _last_context(self, history: list) -> dict:
        """
        The filters the previous answer actually used.

        Carries counterparty, period AND direction, not just the counterparty:
        "show me these transactions" after "how much on Swiggy last month"
        needs the window too, or it silently widens to all time.
        """
        for h in reversed(history or []):
            ctx = h.get("context") or {}
            if ctx.get("merchant") or ctx.get("period"):
                return ctx
        return {}

    def _inherit_context(self, message, tool, args, history) -> dict:
        """
        Fills unspecified arguments from the previous answer.

        A follow-up like "show me these transactions" carries no counterparty
        and no period of its own. Without inheritance the query widens to
        everything, which looks like an answer and is not one.

        Inheritance requires POSITIVE evidence that this is a follow-up, not
        merely the absence of detail. An earlier version inherited whenever the
        question named no period, which made "what was my last transaction in
        general" silently continue to mean Swiggy.

        A turn continues the previous one only when it:
          - refers back ("show me THESE transactions"), or
          - is elliptical ("what about April?", "and last month?")

        And never when it widens the scope ("in general", "overall", "any",
        "across all"), never for a ranking or balance question, and never over
        something the user named this turn.

        This also acts as a correction on the LLM planner, which sees recent
        history and sometimes carries the counterparty forward on its own: an
        explicitly general question drops the counterparty even if the planner
        supplied one.
        """
        inherited = {}
        if tool in ("rank_counterparties", "get_balances"):
            return inherited

        named_merchant = extract_merchant(message, self.vocabulary)
        generic = bool(_GENERIC_SCOPE.search(message or ""))

        # Scope reset: an explicitly general question is not about the last
        # counterparty, whoever proposed it.
        if generic and not named_merchant and args.get("merchant"):
            inherited["dropped_merchant"] = args.pop("merchant")

        prev = self._last_context(history)
        if not prev or generic:
            return inherited

        refers_back = bool(_ANAPHORA.search(message or ""))
        elliptical = bool(_ELLIPTICAL.search(message or ""))
        own_period = extract_period(message)
        is_followup = refers_back or elliptical

        if (is_followup and not args.get("merchant") and not named_merchant
                and prev.get("merchant")):
            args["merchant"] = prev["merchant"]
            inherited["merchant"] = prev["merchant"]

        if (is_followup and not args.get("period") and not own_period
                and prev.get("period_token")):
            args["period"] = prev["period_token"]
            inherited["period"] = prev["period_token"]

        if is_followup and not args.get("direction") and prev.get("direction"):
            args["direction"] = prev["direction"]
            # Marks this as carried from a turn where the user WAS explicit, so
            # a listing keeps the previous direction instead of widening to
            # both. "Show me these" means the debits we just discussed.
            args["_direction_inherited"] = True
            inherited["direction"] = prev["direction"]

        return inherited

    def _confidence(self, resolution, tr, explicit_period, result, narration,
                    via_sql: bool = False) -> Confidence:
        """
        Composite of interpretation risk. See ARCHITECTURE_V2.md §13.

        Four independent signals, each a factor in [0, 1], multiplied together:
          1. counterparty resolution  -- the resolver's own confidence
          2. period assumption        -- did we invent the window
          3. data attribution         -- share of in-scope rows with no
                                         identifiable counterparty
          4. narration integrity      -- did the model's wording survive the
                                         grounding check

        Truncation is deliberately NOT a penalty: the displayed rows are capped
        but the reported total covers every row, so the answer is complete. It
        is still surfaced as a reason, because a reader who assumes the table
        is the whole story would misread it.
        """
        score, reasons = 1.0, []

        # 1. Counterparty resolution.
        if resolution is not None and resolution.status == resolver.MATCH:
            if resolution.confidence >= 1.0:
                reasons.append(f"Matched '{resolution.entity}' exactly")
            else:
                score *= resolution.confidence
                reasons.append(
                    f"Matched '{resolution.entity}' by "
                    f"{resolution.method.replace('_', ' ')} rather than an exact name")

        # 2. Period assumption. Saying "total" counts as explicit; saying
        #    nothing at all means we chose the window on the user's behalf.
        if tr is not None and tr.status == ALL_TIME and not explicit_period:
            score *= PERIOD_ASSUMED_FACTOR
            reasons.append("No time period given — answered over all available history")
        elif tr is not None and tr.status == RESOLVED:
            reasons.append(f"Period resolved to {tr.label}")
        elif tr is not None and tr.status == ALL_TIME:
            reasons.append("Answered over all available history, as asked")

        # 3. Data attribution. Irrelevant for a query that never read
        #    transactions (an accounts lookup via generated SQL), so skipped.
        reads_txns = not (via_sql and result is not None
                          and sqlguard.VIEW_TXN not in (result.sql or "").lower())
        if result is not None and reads_txns:
            unattributed = self._unattributed_share(
                (result.filters or {}).get("direction"), tr)
            if unattributed > 0.02:
                score *= (1.0 - unattributed)
                reasons.append(
                    f"{unattributed:.0%} of transactions in this window have no "
                    f"identifiable counterparty and cannot be attributed to a merchant")
            if result.truncated:
                reasons.append(
                    f"Showing {len(result.rows)} of {result.total_group_count} rows — "
                    f"the total covers all of them")

        # 4. Narration integrity.
        if narration == "llm_rejected":
            score *= NARRATION_REJECTED_FACTOR
            reasons.append("The model produced a figure the database did not return; "
                           "its wording was discarded and a verified summary used")

        # 5. Generated SQL. The database still did the arithmetic and the
        #    sandbox still scoped the data, but the question -> query mapping
        #    was the model's alone, with no typed contract to catch a
        #    misreading. Say so, and band it down.
        if via_sql:
            score *= VIA_SQL_FACTOR
            reasons.append("Answered with model-written SQL (sandboxed and scoped to "
                           "your accounts) because no built-in tool matched the "
                           "question — check the query in the audit trace")

        score = round(max(0.0, min(1.0, score)), 3)
        return Confidence(score=score, label=band_for(score), reasons=reasons)

    def _unattributed_share(self, direction, tr) -> float:
        """Share of in-scope transactions whose counterparty could not be parsed."""
        try:
            where = ["entity_id = ?"]
            params = [self.entity_id]
            if direction in config.VALID_TXN_TYPES:
                where.append("transaction_type = ?")
                params.append(direction)
            if tr is not None and tr.status == RESOLVED:
                where.append("txn_month BETWEEN ? AND ?")
                params.extend([tr.start, tr.end])
            df = db.query_df(self.con, f"""
                SELECT COALESCE(SUM(txn_count) FILTER (
                           WHERE merchant_norm = '{config.UNKNOWN_MERCHANT}'), 0) AS unknown,
                       COALESCE(SUM(txn_count), 0) AS total
                FROM {config.TABLE_ROLLUP_MONTHLY} WHERE {' AND '.join(where)}
            """, params)
            if df.empty or not float(df.iloc[0]["total"]):
                return 0.0
            return float(df.iloc[0]["unknown"]) / float(df.iloc[0]["total"])
        except Exception:
            return 0.0

    # ---------- gates ----------

    def _resolve_merchant(self, name):
        if not name:
            return None, None
        res = resolver.resolve_merchant(self.con, self.entity_id, name,
                                        vocabulary=self.vocabulary)
        return res.entity, res

    def _resolve_period(self, token):
        if token is None:
            return db.TimeRange(status=ALL_TIME, label="all time", canonical="all_time"), False
        tr = db.resolve_time_range(token, self.anchor)
        return tr, True

    # A singular possessive reference to one unnamed individual.
    _SINGULAR_PERSON = re.compile(
        r"\b(?:my|a)\s+(friend|buddy|mate|colleague|roommate|flatmate|"
        r"brother|sister|cousin|dad|mum|mom|father|mother|landlord)\b", re.I)

    def _gate_singular_person(self, message, args, tool, canonical=None):
        """
        Asks which individual, when the user referred to exactly one.

        Runs before tool dispatch rather than inside the ranking branch: the
        planner is not consistent about which tool "how much did my friend pay
        me" becomes, and a gate that only guards one path is not a guarantee.
        """
        if args.get("_skip_person_gate") or canonical:
            return None
        if not self._SINGULAR_PERSON.search(message or ""):
            return None
        if any(p.lower() in (message or "").lower() for p in self._people):
            return None  # they named someone
        ranked = sorted(
            (v for v in self.vocabulary if v["kind"] == config.KIND_PERSON),
            key=lambda v: -float(v.get("total_credit") or 0))
        if not ranked:
            return None
        return AgentResult(
            status=CLARIFY,
            question=("Which person did you mean? These individuals have sent you "
                      "money — or pick *Everyone* for the combined total."),
            options=[v["name"] for v in ranked[:5]] + ["Everyone"],
            pending={"slot": "merchant", "tool": tool,
                     "direction": config.TXN_CREDIT,
                     "period": args.get("period"), "kind": config.KIND_PERSON},
        )

    # A request that is already bounded some other way: by recency ("last 5"),
    # by an extreme ("highest"), by a threshold ("over 1 lakh"), or by an
    # explicit count. Asking "which period?" for these is not clarification --
    # it is a question the user already answered.
    _BOUNDED = re.compile(
        r"\b(last|latest|most recent|recent|first|earliest|oldest|highest|lowest|"
        r"largest|smallest|biggest|max(?:imum)?|min(?:imum)?|top|bottom)\b|"
        r"\b(more|greater|less|fewer|higher|lower)\s+than\b|\b(over|above|below|under|"
        r"exceed(?:s|ing)?|at least|at most)\s+(?:₹|rs\.?|inr)?\s*[\d,.]+", re.I)

    def _is_bounded_listing(self, message: str, args: dict, tool: str) -> bool:
        """
        True when a listing is already bounded without a period: by recency
        ("last 5"), an extreme ("highest"), a threshold ("over 1 lakh"), or an
        explicit small count.

        Shared by the period gate (which must not ask) and by confidence (which
        must not penalise): for these questions all-time is the CORRECT scope,
        not an assumption made on the user's behalf.
        """
        if tool != "list_transactions":
            return False
        args = args or {}
        limit = args.get("limit")
        try:
            small = limit is not None and int(limit) <= 10
        except (TypeError, ValueError):
            small = False
        return bool(args.get("min_amount") is not None
                    or args.get("max_amount") is not None
                    or args.get("order_by") == "amount"
                    or small
                    or self._BOUNDED.search(message or ""))

    # Values that are legitimately string params in generated SQL but are not
    # counterparty names, so they must not be pushed through the resolver.
    _SQL_ENUM_VALUES = frozenset(
        v.lower() for v in (config.TXN_DEBIT, config.TXN_CREDIT,
                            config.KIND_MERCHANT, config.KIND_PERSON,
                            config.KIND_BANK_CHARGE, config.KIND_SELF_TRANSFER,
                            config.KIND_UNKNOWN, config.CHANNEL_UPI, config.CHANNEL_NEFT,
                            config.CHANNEL_IMPS, config.CHANNEL_RTGS, config.CHANNEL_FT,
                            config.CHANNEL_CHARGE, config.CHANNEL_OTHER))
    _SQL_LITERAL = re.compile(r"'((?:[^']|'')+)'")
    _DATE_LIKE = re.compile(r"^\d{4}-\d{2}(-\d{2})?")

    def _sql_counterparty_guard(self, message: str, sql: str, params: list):
        """
        Keeps generated SQL from being a way around entity resolution.

        Without this, "what did I spend on Oracle?" could become
        `WHERE merchant = 'ORACLE'`, return zero rows, and be narrated as
        "you spent nothing" -- when the truth is that this customer has never
        transacted with anyone called Oracle. Every string the model used as a
        filter is resolved against the customer's real vocabulary:

            NOT_FOUND  -> the same guardrail the typed tools produce
            AMBIGUOUS  -> the same clarifying question
            MATCH      -> the canonical name is substituted, so 'swiggy' finds
                          SWIGGY and 'Bundl Technologies' finds it too

        Returns (sql, params, status, resolution, name_as_given).
        """
        if not self.vocabulary:
            return sql, params, None, None, None
        candidates = []
        for p in params or []:
            if isinstance(p, str):
                candidates.append(("param", p))
        for lit in self._SQL_LITERAL.findall(sql or ""):
            candidates.append(("literal", lit.replace("''", "'")))

        new_sql, new_params = sql, list(params or [])
        for where, raw in candidates:
            s = str(raw).strip()
            low = s.lower()
            if (not s or len(s) < 2 or low in self._SQL_ENUM_VALUES
                    or self._DATE_LIKE.match(s) or re.fullmatch(r"[\d.,%\s-]+", s)
                    or looks_like_period(s)):
                continue
            res = resolver.resolve_merchant(self.con, self.entity_id, s,
                                            vocabulary=self.vocabulary)
            if res.status == resolver.NOT_FOUND:
                return sql, params, resolver.NOT_FOUND, res, s
            if res.status == resolver.AMBIGUOUS:
                return sql, params, resolver.AMBIGUOUS, res, s
            if res.status == resolver.MATCH and res.entity and res.entity != s:
                if where == "param":
                    new_params = [res.entity if (isinstance(p, str) and p == raw) else p
                                  for p in new_params]
                else:
                    new_sql = new_sql.replace(f"'{raw}'", "'" + res.entity.replace("'", "''") + "'")
        return new_sql, new_params, None, None, None

    def _gate_period(self, merchant_canonical, period_token, tr, explicit,
                     message: str = None, args: dict = None, tool: str = None):
        """
        Policy gate: a counterparty with history across several months, and no
        period given, is ambiguous in a way that matters.

        This is the flow from the brief:
            user:  I want to calculate my spending for swiggy
            agent: for which months? past n months or all?

        The gate exists for AGGREGATES -- "how much" over an open-ended window
        is genuinely ambiguous. A listing that is already bounded by recency,
        an extreme, a threshold or an explicit count is not, so it passes:
        "last 5 transactions with BookMyShow" needs no period to be answerable.
        """
        if explicit or not merchant_canonical:
            return None
        if self._is_bounded_listing(message, args, tool):
            return None
        stats = next((v for v in self.vocabulary if v["name"] == merchant_canonical), None)
        months = int(stats.get("active_months") or 0) if stats else 0
        if months <= 1:
            return None
        first = str(stats.get("first_month"))[:7] if stats else ""
        last = str(stats.get("last_month"))[:7] if stats else ""
        return AgentResult(
            status=CLARIFY,
            question=(f"I have **{months} months** of activity for "
                      f"**{merchant_canonical}** ({first} to {last}). "
                      f"Which period would you like?"),
            options=["Last month", "Last 3 months", "Last 6 months",
                     "This year", "All time"],
            pending={"slot": "period", "merchant": merchant_canonical},
        )

    # ---------- execution ----------

    def run(self, message: str, history: list = None, pending: dict = None,
            thread_id: str = None, turn: int = None) -> AgentResult:
        """
        Runs one turn through the LangGraph state machine.

        The public contract is unchanged from the hand-rolled version: same
        arguments, same AgentResult. `thread_id` and `turn` are optional and
        only affect where checkpoints are written.
        """
        t0 = time.perf_counter()
        # One slot per turn; nodes park rich objects here instead of in state.
        run_id = uuid.uuid4().hex
        self._scratch[run_id] = {}
        state = {"message": message, "history": history or [],
                 "pending_in": pending or {}, "trace": [], "run_id": run_id}

        # `thread_id` here is the CONVERSATION id. The graph gets a derived
        # per-turn thread instead, because a LangGraph thread is one continuing
        # execution while a chat is many executions that share context.
        #
        # Reusing the conversation id as the graph thread makes turn 2 resume
        # turn 1's finished state: every node sees `status` already set, routes
        # straight to END, and the previous answer comes back verbatim.
        # `checkpoint_ns` does not help -- it is reserved for subgraphs and is
        # normalised to '' at the top level. Deriving `conv#turn` keeps a
        # conversation's checkpoints greppable together (and deletable with a
        # LIKE) while giving every turn a clean slate.
        #
        # A checkpointer also makes the config mandatory, so callers without a
        # conversation (tests, scripts) get an ephemeral id rather than an error.
        graph_thread = (f"{thread_id}#{turn if turn is not None else uuid.uuid4().hex[:8]}"
                        if thread_id else f"ephemeral#{uuid.uuid4().hex}")
        cfg = {"configurable": {"thread_id": graph_thread}}
        try:
            final = self._graph.invoke(state, config=cfg)
        except UnresolvedFilterError as e:
            final = {
                "status": CLARIFY,
                "question": f"I couldn't work out the time period "
                            f"**'{e.value}'**. Which period did you mean?",
                "options": ["Last month", "Last 3 months", "This year", "All time"],
                "pending_out": {"slot": "period"},
                "trace": state["trace"],
            }
        except Exception as e:
            final = {"status": ERROR, "answer": f"Something went wrong: {e}",
                     "trace": state["trace"]}

        # Carry the ORIGINAL question through a clarification. The reply that
        # resumes it ("no only swiggy", "last 3 months") is not a question, and
        # a narrator handed only that text invents one -- it once explained
        # that there were "no transactions with Swiggy" after the user had just
        # asked to drop the Swiggy filter.
        # Collect the rich objects the nodes parked for this turn, and free the
        # slot whether the turn succeeded, clarified, or raised.
        sc = self._scratch.pop(run_id, {})

        pend = dict(final.get("pending_out") or {})
        if final.get("status") == CLARIFY:
            pend.setdefault("original", (pending or {}).get("original") or message)

        return AgentResult(
            status=final.get("status", ERROR),
            answer=final.get("answer", ""),
            question=final.get("question", ""),
            options=final.get("options", []) or [],
            result=sc.get("result"),
            kind=final.get("result_kind", ""),
            trace=final.get("trace", []) or [],
            planner=final.get("planner", ""),
            narration=final.get("narration", ""),
            pending=pend,
            resolution=sc.get("resolution"),
            confidence=sc.get("confidence") or Confidence(),
            inherited=final.get("inherited", {}) or {},
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        )

    def _resume(self, message: str, pending: dict):
        """Turns a reply to a clarifying question back into a tool call."""
        slot = pending.get("slot")
        tool = pending.get("tool", "get_spend")
        args = {k: v for k, v in pending.items()
                if k not in ("slot", "question", "options", "tool", "original")}
        if slot == "period":
            reply = (message or "").strip().lower()
            # "no", "all", "doesn't matter" answer the question -- with "no
            # restriction". Without this the same clarification is re-asked in
            # a loop, which is what happened with "no only swiggy".
            if _NO_PERIOD_REPLY.search(reply):
                args["period"] = "all_time"
                # "not only swiggy" / "all vendors" also widens the counterparty.
                if _WIDEN_SCOPE_REPLY.search(reply):
                    args.pop("merchant", None)
                    if tool == "get_spend":
                        tool = "list_transactions"
                return tool, {**args, "direction": args.get("direction", config.TXN_DEBIT)}
            token = extract_period(message) or extract_period(f"in {message}")
            if token is None:
                norm = re.sub(r"[\s\-]+", "_", reply)
                probe = db.resolve_time_range(norm, self.anchor)
                token = norm if probe.status != UNRESOLVED else None
            if token is None:
                return None
            args["period"] = token
        elif slot == "merchant":
            reply = message.strip()
            if reply.lower() in {"everyone", "everybody", "all", "all of them", "combined"}:
                # They want the aggregate after all -- keep the ranking tool but
                # do not re-ask the same question.
                args.pop("merchant", None)
                args["_skip_person_gate"] = True
            else:
                args["merchant"] = reply
                # A specific counterparty turns a ranking into a single total.
                if tool == "rank_counterparties":
                    tool = "get_spend"
        elif slot == "account":
            args["account"] = message.strip()
        args.setdefault("direction", config.TXN_DEBIT)
        return tool, args
