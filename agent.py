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
from dataclasses import dataclass, field

import config
import db
import queries
import resolver
import explainer
from db import RESOLVED, ALL_TIME, UNRESOLVED
from queries import UnresolvedFilterError

ANSWER = "ANSWER"
CLARIFY = "CLARIFY"
GUARDRAIL = "GUARDRAIL"
ERROR = "ERROR"

MAX_ITERATIONS = 4


@dataclass
class Confidence:
    """
    How much to trust this answer.

    The arithmetic is always exact -- it comes from SQL. What varies is whether
    the question was interpreted correctly: did we pick the right counterparty,
    the right window, and is the underlying data fully attributed. Those are the
    things this scores, and each one is reported with its reason so the number
    is inspectable rather than decorative.
    """
    score: float = 1.0
    label: str = "High"
    reasons: list = field(default_factory=list)

    @property
    def pct(self) -> int:
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
               "'april', or 'all_time' when the user says total/overall/ever. "
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
            "period_a": {"type": "string", "description": "Earlier period. " + PERIOD_DESC},
            "period_b": {"type": "string", "description": "Later period. " + PERIOD_DESC},
            "direction": {"type": "string", "enum": ["debit", "credit"]},
        }, "required": ["period_a", "period_b", "direction"]}}},

    {"type": "function", "function": {
        "name": "list_transactions",
        "description": "Individual transactions. Use for 'show me those', 'list my "
                       "payments to X'.",
        "parameters": {"type": "object", "properties": {
            "merchant": {"type": "string"},
            "period": {"type": "string", "description": PERIOD_DESC},
            "direction": {"type": "string", "enum": ["debit", "credit"]},
            "limit": {"type": "integer"},
        }, "required": []}}},

    {"type": "function", "function": {
        "name": "get_balances",
        "description": "Current account balances. Use for 'what is my balance', "
                       "'how much do I have'.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},

    {"type": "function", "function": {
        "name": "ask_user",
        "description": "Ask the user a clarifying question when the request is "
                       "genuinely ambiguous. Prefer this over guessing.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        }, "required": ["question"]}}},
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
- "my friend paid me" -> rank_counterparties(direction='credit', kind='person')
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

_ALL_TIME = re.compile(r"\b(in total|total|overall|all time|all-time|ever|lifetime|"
                       r"altogether|so far)\b", re.I)

_PERIOD_PATTERNS = [
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
        if cand.lower() not in {"me", "my", "it", "that", "them", "total", "all",
                                "the most", "most", "everything"}:
            return cand
    return None


# =============================================================
# AGENT
# =============================================================

class FinanceAgent:
    def __init__(self, con, entity_id=None, session=None):
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

    # ---------- planning ----------

    def _plan_llm(self, message: str, history: list):
        key = os.getenv("GROQ_API_KEY", config.GROQ_API_KEY)
        if not key:
            return None
        try:
            from groq import Groq
            msgs = [{"role": "system",
                     "content": SYSTEM_PROMPT.format(anchor=self.anchor)}]
            for h in history[-4:]:
                if h.get("role") in ("user", "assistant") and h.get("content"):
                    msgs.append({"role": h["role"], "content": str(h["content"])[:500]})
            msgs.append({"role": "user", "content": message})

            resp = Groq(api_key=key).chat.completions.create(
                model=config.ACTIVE_MODEL, messages=msgs,
                tools=TOOLS, tool_choice="auto", temperature=0.0, max_tokens=400,
                reasoning_effort="low",
            )
            calls = resp.choices[0].message.tool_calls
            if not calls:
                return None
            call = calls[0]
            args = json.loads(call.function.arguments or "{}")
            return call.function.name, args
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
        if re.search(r"\b(balances?|how much do i have|in my account)\b"
                     r"|\b(?:all\s+)?(?:my\s+)?accounts?\b", low):
            return "get_balances", {}

        if re.search(r"\b(compare|versus|vs\.?|difference between)\b", low) or \
           re.search(r"\bhow (?:does|did) (?:that|this|it) compare\b", low):
            prev = self._last_context(history)
            return "compare_spend", {
                "merchant": merchant or prev.get("merchant"),
                "period_a": "two_months_ago", "period_b": period or "last_month",
                "direction": direction,
            }

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

        # "What was my last transaction?" wants the most recent rows, not a
        # spend total. Without this it lands on get_spend and reports a sum.
        if re.search(r"\b(last|latest|most recent|recent)\b[\w\s]{0,15}\btransactions?\b", low):
            return "list_transactions", {
                "merchant": merchant, "period": period, "limit": 5}

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

    def _confidence(self, resolution, tr, explicit_period, result, narration) -> Confidence:
        """Composite of interpretation risks. The arithmetic itself is exact."""
        score, reasons = 1.0, []

        if resolution is not None and resolution.status == resolver.MATCH:
            if resolution.confidence < 1.0:
                score = min(score, max(resolution.confidence, 0.75))
                reasons.append(
                    f"'{resolution.entity}' matched by {resolution.method.replace('_', ' ')} "
                    f"({int(resolution.confidence * 100)}%)")
            else:
                reasons.append(f"Exact match on '{resolution.entity}'")

        if tr is not None and tr.status == ALL_TIME and not explicit_period:
            score = min(score, 0.9)
            reasons.append("No period given — answered over all available history")
        elif tr is not None and tr.status == RESOLVED:
            reasons.append(f"Period resolved to {tr.label}")

        if result is not None:
            filters = result.filters or {}
            unattributed = self._unattributed_share(filters.get("direction"), tr)
            if unattributed > 0.02:
                score = min(score, 1.0 - min(unattributed, 0.25))
                reasons.append(
                    f"{unattributed:.0%} of transactions in range have no identifiable "
                    f"counterparty and are excluded from per-merchant figures")
            if result.truncated:
                reasons.append(
                    f"Displaying {len(result.rows)} of {result.total_group_count} rows "
                    f"(the total covers all of them)")

        if narration == "llm_rejected":
            reasons.append("Model wording was discarded for an unverifiable figure; "
                           "a database-generated summary was used")

        label = "High" if score >= 0.9 else ("Moderate" if score >= 0.75 else "Low")
        return Confidence(score=round(score, 2), label=label, reasons=reasons)

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

    def _gate_period(self, merchant_canonical, period_token, tr, explicit):
        """
        Policy gate: a counterparty with history across several months, and no
        period given, is ambiguous in a way that matters.

        This is the flow from the brief:
            user:  I want to calculate my spending for swiggy
            agent: for which months? past n months or all?
        """
        if explicit or not merchant_canonical:
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

    def run(self, message: str, history: list = None, pending: dict = None) -> AgentResult:
        t0 = time.perf_counter()
        history = history or []
        trace = []

        # Resuming a clarification: fill the missing slot from this reply.
        if pending and pending.get("slot"):
            merged = self._resume(message, pending)
            if merged is None:
                return AgentResult(
                    status=CLARIFY, question=pending.get("question", "Which period?"),
                    options=pending.get("options", []), pending=pending,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2))
            tool, args = merged
            planner = "resume"
        else:
            plan = self._plan_llm(message, history)
            planner = "llm"
            if plan is None:
                plan = self._plan_rules(message, history)
                planner = "rules"
            tool, args = plan

        trace.append({"step": "plan", "planner": planner, "tool": tool, "args": dict(args)})

        inherited = {}
        if planner != "resume":
            inherited = self._inherit_context(message, tool, args, history)
            if inherited:
                trace.append({"step": "inherit_context", "from_previous_turn": inherited})

        try:
            out = self._execute(message, tool, args, trace)
        except UnresolvedFilterError as e:
            out = AgentResult(
                status=CLARIFY,
                question=f"I couldn't work out the time period **'{e.value}'**. "
                         f"Which period did you mean?",
                options=["Last month", "Last 3 months", "This year", "All time"],
                pending={"slot": "period", **{k: v for k, v in args.items() if k != "period"},
                         "tool": tool},
            )
        except Exception as e:
            out = AgentResult(status=ERROR, answer=f"Something went wrong: {e}")

        out.trace = trace
        out.planner = planner
        out.inherited = inherited
        out.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return out

    def _resume(self, message: str, pending: dict):
        """Turns a reply to a clarifying question back into a tool call."""
        slot = pending.get("slot")
        tool = pending.get("tool", "get_spend")
        args = {k: v for k, v in pending.items()
                if k not in ("slot", "question", "options", "tool")}
        if slot == "period":
            token = extract_period(message) or extract_period(f"in {message}")
            if token is None:
                norm = re.sub(r"[\s\-]+", "_", message.strip().lower())
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
        args.setdefault("direction", config.TXN_DEBIT)
        return tool, args

    def _execute(self, message, tool, args, trace) -> AgentResult:
        if tool == "ask_user":
            return AgentResult(status=CLARIFY,
                               question=args.get("question", "Could you clarify?"),
                               options=args.get("options", []),
                               pending={"slot": "period", "tool": "get_spend"})

        if tool == "get_balances":
            all_accounts = bool(re.search(
                r"\b(all|every|each|other|total|combined|across)\b.{0,20}\baccounts?\b|"
                r"\baccounts?\b.{0,20}\b(all|each|list)\b", message or "", re.I))
            r = queries.get_balances(self.con, self.entity_id,
                                     account_id=self.session.account_id,
                                     all_accounts=all_accounts)
            trace.append({"step": "query", "tool": tool, "sql": r.display_sql(),
                          "rows": len(r.rows), "ms": r.latency_ms, "source": r.source,
                          "account": "all" if all_accounts else "primary"})
            answer, method = explainer.generate(message, "balances", r)
            return AgentResult(ANSWER, answer=answer, result=r, kind="balances",
                               narration=method,
                               confidence=Confidence(1.0, "High",
                                                     ["Balance read directly from the account record"]))

        stated = explicit_direction(message)
        direction = stated or args.get("direction")
        if tool == "list_transactions" and not stated and not args.get("_direction_inherited"):
            # "What was my last transaction?" means either direction. Letting a
            # planner's guess stand here would silently hide an incoming
            # payment, so only an explicit word narrows a listing.
            direction = None
        elif direction not in config.VALID_TXN_TYPES:
            direction = config.TXN_DEBIT

        # ---- counterparty resolution + guardrails ----
        raw_merchant = args.get("merchant")
        kind = args.get("kind")
        canonical, res = None, None

        if raw_merchant:
            if str(raw_merchant).strip().lower() in {
                    "friend", "my friend", "someone", "a friend", "person"}:
                p = resolver.resolve_person(self.con, self.entity_id, raw_merchant)
                trace.append({"step": "resolve_person", "status": p.status,
                              "candidates": p.candidates})
                if p.status == resolver.AMBIGUOUS:
                    return AgentResult(
                        status=CLARIFY,
                        question="Which person did you mean? These people have sent "
                                 "you money:",
                        options=p.candidates,
                        pending={"slot": "merchant", "tool": tool,
                                 "direction": config.TXN_CREDIT,
                                 "period": args.get("period")},
                        resolution=p)
                raw_merchant = None
                kind = config.KIND_PERSON
            else:
                canonical, res = self._resolve_merchant(raw_merchant)
                trace.append({"step": "resolve_merchant", "input": raw_merchant,
                              "status": res.status if res else None,
                              "resolved": canonical,
                              "confidence": res.confidence if res else None,
                              "method": res.method if res else None})
                if res and res.status == resolver.AMBIGUOUS:
                    return AgentResult(
                        status=CLARIFY, question=resolver.describe(res),
                        options=res.candidates,
                        pending={"slot": "merchant", "tool": tool,
                                 "direction": direction, "period": args.get("period")},
                        resolution=res)
                if res and res.status == resolver.NOT_FOUND:
                    near = (f" The closest names on record are "
                            f"{', '.join(res.candidates[:3])}." if res.candidates else "")
                    return AgentResult(
                        status=GUARDRAIL,
                        answer=f"I have no transactions for **{raw_merchant}**.{near}",
                        resolution=res,
                        confidence=Confidence(
                            1.0, "High",
                            [f"'{raw_merchant}' is not present in this customer's "
                             f"transaction history"]))

        # Policy gate: "how much did MY FRIEND pay me" asks about ONE person.
        # Totalling every individual answers a different question while reading
        # as authoritative, so offer the names instead. Checked here so it
        # applies whichever tool the planner chose.
        person_gate = self._gate_singular_person(message, args, tool, canonical)
        if person_gate is not None:
            trace.append({"step": "policy_gate", "gate": "singular_person"})
            return person_gate

        # ---- period resolution + gate ----
        if tool == "compare_spend":
            tr_a, _ = self._resolve_period(args.get("period_a"))
            tr_b, _ = self._resolve_period(args.get("period_b"))
            for tr in (tr_a, tr_b):
                if tr.status == UNRESOLVED:
                    raise UnresolvedFilterError("time period", tr.label, tr.suggestions)
            r = queries.compare_periods(self.con, self.entity_id, direction,
                                        tr_a, tr_b, merchant=canonical)
            trace.append({"step": "query", "tool": tool, "sql": r.display_sql(),
                          "rows": len(r.rows), "ms": r.latency_ms, "source": r.source})
            answer, method = explainer.generate(message, "compare", r, res)
            return AgentResult(
                ANSWER, answer=answer, result=r, kind="compare", narration=method,
                resolution=res,
                confidence=self._confidence(res, tr_b, True, r, method),
                pending={"merchant": canonical, "period_token": args.get("period_b"),
                         "direction": direction})

        period_token = args.get("period")
        tr, explicit = self._resolve_period(period_token)
        if tr.status == UNRESOLVED:
            raise UnresolvedFilterError("time period", tr.label, tr.suggestions)

        gate = self._gate_period(canonical, period_token, tr, explicit)
        if gate is not None:
            gate.pending.update({"tool": tool, "direction": direction})
            gate.resolution = res
            trace.append({"step": "policy_gate", "gate": "period_required",
                          "merchant": canonical})
            return gate

        trace.append({"step": "resolve_period", "input": period_token,
                      "status": tr.status, "window": f"{tr.start}..{tr.end}",
                      "label": tr.label, "month_aligned": tr.month_aligned})

        if tool == "rank_counterparties":
            r = queries.top_counterparties(self.con, self.entity_id, direction, tr,
                                           limit=int(args.get("limit") or 10), kind=kind)
            k = "rank"
        elif tool == "list_transactions":
            r = queries.list_transactions(self.con, self.entity_id, direction, tr,
                                          merchant=canonical, kind=kind,
                                          limit=int(args.get("limit") or 50))
            k = "list"
        else:
            r = queries.query_spend(self.con, self.entity_id, direction, tr,
                                    merchant=canonical, kind=kind)
            k = "spend"

        trace.append({"step": "query", "tool": tool, "sql": r.display_sql(),
                      "rows": len(r.rows), "ms": r.latency_ms, "source": r.source,
                      "grand_total": r.facts.get("grand_total"),
                      "truncated": r.truncated})

        answer, method = explainer.generate(message, k, r, res)
        trace.append({"step": "narrate", "method": method})
        conf = self._confidence(res, tr, explicit, r, method)
        trace.append({"step": "confidence", "score": conf.score, "label": conf.label,
                      "reasons": conf.reasons})
        # Everything a follow-up may need to inherit.
        ctx = {"direction": direction}
        if canonical:
            ctx["merchant"] = canonical
        if period_token:
            ctx["period_token"] = period_token
        elif tr.status == RESOLVED and tr.canonical:
            ctx["period_token"] = tr.canonical
        return AgentResult(ANSWER, answer=answer, result=r, kind=k,
                           narration=method, resolution=res,
                           confidence=conf, pending=ctx)
