"""
Grounded Explainer (Phase 4)
============================
Turns a pre-computed QueryResult into plain language.

The model narrates; it never calculates. Two mechanisms enforce that rather
than requesting it:

  1. It is handed a FACTS block computed over the full result set, and the
     displayed rows are explicitly labelled a sample. V1 passed df.head(10) and
     asked for a description of the whole thing (BUGS.md B05).

  2. Every number it writes is verified against those facts before the answer
     is shown. If a figure appears that the database did not produce, the LLM
     text is discarded and a deterministic template is rendered instead.

(2) is the part V1 lacked entirely. The old prompt *asked* the model not to
invent numbers and nothing checked. Here an ungrounded figure cannot reach the
user, which turns "we ask it not to hallucinate" into "it cannot".
"""

import os
import re

import config
import security

# Currency-shaped figures: 1,234.56 / 1234.56 / 12,345
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Small bare integers are counts, years, ordinals and list markers -- verifying
# them produces false rejections without catching real fabrication.
_TRIVIAL_MAX = 3000


def _facts_numbers(facts: dict, rows) -> set:
    """Every number the database actually produced for this answer."""
    allowed = set()

    def add(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if f != f:  # NaN
            return
        allowed.add(round(abs(f), 2))
        allowed.add(round(abs(f)))

    for v in (facts or {}).values():
        if isinstance(v, (int, float)):
            add(v)
    if rows is not None and not rows.empty:
        for col in rows.select_dtypes("number").columns:
            for v in rows[col].dropna():
                add(v)
    return allowed


def verify_grounding(answer: str, facts: dict, rows) -> tuple:
    """
    Returns (ok, offending_numbers).

    A figure passes if the database produced it, or if it is a small integer
    (a count or a year). Percentages are checked against the facts too.
    """
    allowed = _facts_numbers(facts, rows)
    bad = []
    for tok in _NUM.findall(answer or ""):
        try:
            val = float(tok.replace(",", ""))
        except ValueError:
            continue
        r2, r0 = round(val, 2), round(val)
        if r2 in allowed or r0 in allowed:
            continue
        if val <= _TRIVIAL_MAX and float(val).is_integer():
            continue
        bad.append(tok)
    return (len(bad) == 0), bad

def normalise_amounts(text: str, facts: dict, rows) -> str:
    """
    Rewrites grounded figures into the canonical currency format.

    Models restate '127,896.90' as '127896.9' however firmly the prompt asks
    otherwise. Reformatting after verification is safe because only numbers
    that already matched a database value are touched -- this cannot introduce
    a figure, only tidy one.
    """
    allowed = _facts_numbers(facts, rows)
    if not allowed:
        return text

    def repl(m):
        tok = m.group(0)
        try:
            val = float(tok.replace(",", ""))
        except ValueError:
            return tok
        if val < 1000 or round(val, 2) not in allowed:
            return tok
        return f"{val:,.2f}"

    sym = re.escape(config.CURRENCY_SYMBOL)
    return re.sub(rf"(?<={sym})\s?\d[\d,]*(?:\.\d+)?", lambda m: repl(m), text)


_AMOUNT_KEYS = {"grand_total", "total_a", "total_b", "delta", "average",
                "top_amount", "excluded_total", "max_amount", "avg_amount"}


def format_facts(facts: dict) -> dict:
    """
    Renders amount-like values as formatted currency strings before they reach
    the model, so it copies '{sym}3,502,747.90' rather than restating a bare
    float as '3502747.9'. Verification strips separators, so formatting here
    does not weaken the grounding check.
    """
    out = {}
    for k, v in (facts or {}).items():
        if v is None:
            continue
        out[k] = money(v) if (k in _AMOUNT_KEYS and isinstance(v, (int, float))) else v
    return out


def money(v) -> str:
    try:
        return f"{config.CURRENCY_SYMBOL}{float(v):,.2f}"
    except (TypeError, ValueError):
        return f"{config.CURRENCY_SYMBOL}0.00"


# =============================================================
# DETERMINISTIC TEMPLATES  (always correct, always available)
# =============================================================

def template_answer(kind: str, result, resolution=None) -> str:
    f = result.facts
    period = f.get("period") or "all time"

    if kind == "spend":
        name = f.get("merchant")
        who = f" with **{name}**" if name else ""
        if f.get("txn_count", 0) == 0:
            return f"No {f.get('direction', 'debit')} transactions found{who} for {period}."
        direction_word = "spent" if f.get("direction") == config.TXN_DEBIT else "received"
        return (f"You {direction_word} **{money(f['grand_total'])}**{who} in {period}, "
                f"across **{f['txn_count']}** transaction(s), "
                f"averaging {money(f.get('average', 0))}.")

    if kind == "rank":
        if result.rows.empty:
            return f"No activity found for {period}."
        top = result.rows.iloc[0]
        lines = [f"Your largest counterparty in {period} was **{top['merchant_norm']}** "
                 f"at **{money(top['total_amount'])}** "
                 f"over {int(top['txn_count'])} transaction(s)."]
        if len(result.rows) > 1:
            rest = ", ".join(
                f"{r['merchant_norm']} ({money(r['total_amount'])})"
                for _, r in result.rows.iloc[1:4].iterrows())
            lines.append(f"Next: {rest}.")
        lines.append(f"Total across all **{f.get('group_count', len(result.rows))}** "
                     f"counterparties: **{money(f['grand_total'])}**.")
        return " ".join(lines)

    if kind == "compare":
        d, pct = f.get("delta", 0), f.get("pct_change")
        direction = "more" if d > 0 else "less"
        pct_txt = f" ({abs(pct):.1f}% {direction})" if pct is not None else ""
        name = f" on **{f['merchant']}**" if f.get("merchant") else ""
        return (f"You spent **{money(f['total_b'])}**{name} in {f['period_b']}, "
                f"versus **{money(f['total_a'])}** in {f['period_a']} — "
                f"**{money(abs(d))}** {direction}{pct_txt}.")

    if kind == "list":
        if result.rows.empty:
            return f"No transactions found for {period}."
        shown = ""
        if result.truncated:
            shown = f", showing the most recent {len(result.rows)}"
        return (f"Found **{f['txn_count']}** transaction(s) totalling "
                f"**{money(f['grand_total'])}** in {period}{shown}.")

    if kind == "balances":
        if result.rows.empty:
            return "No accounts found."
        total_accounts = f.get("total_accounts", f.get("account_count", 1))
        if f.get("account_count") == 1 and total_accounts > 1:
            extra = (f" You hold **{total_accounts}** accounts in total, with a "
                     f"combined balance of **{money(f.get('combined_balance', 0))}**.")
            return (f"Your primary account ({f.get('account_number', '')}"
                    f"{', ' + f['bank_name'] if f.get('bank_name') else ''}) has an "
                    f"available balance of **{money(f['grand_total'])}**.{extra}")
        return (f"You have **{f['account_count']}** account(s) with a combined "
                f"balance of **{money(f['grand_total'])}**.")

    return f"Result: {money(f.get('grand_total', 0))} across {f.get('txn_count', 0)} transaction(s)."


# =============================================================
# LLM NARRATION
# =============================================================

SYSTEM = """You are a careful personal-finance assistant.

You are given FACTS that a database has already computed, plus a SAMPLE of the
underlying rows. Write a short, natural answer to the user's question.

Rules:
1. Use ONLY numbers that appear in FACTS. Never add, subtract, average or
   recompute anything yourself.
2. The SAMPLE may be partial. Never describe totals or counts from it -- those
   are in FACTS.
3. Two or three sentences. Plain markdown. No tables, no bullet lists.
4. Copy amounts EXACTLY as written in FACTS, including the currency symbol,
   thousands separators and decimal places. Do not reformat them.
5. If NOTES mention exclusions or truncation, mention that briefly.
6. Do not invent context, advice, or commentary. Answer the question.
7. If counterparty_kind is 'person', these are individuals -- call them people,
   never merchants or vendors."""


def generate(user_question: str, kind: str, result, resolution=None) -> tuple:
    """
    Returns (answer_text, method) where method is 'llm', 'llm_rejected' or
    'template'.
    """
    fallback = template_answer(kind, result, resolution)

    key = os.getenv("GROQ_API_KEY", config.GROQ_API_KEY)
    if not key:
        return fallback, "template"

    try:
        from groq import Groq

        sample = []
        if result.rows is not None and not result.rows.empty:
            sample = security.redact_records(
                result.rows.head(8).to_dict(orient="records"))

        prompt = (
            f"USER QUESTION: {user_question}\n\n"
            f"FACTS (authoritative, computed by the database):\n"
            f"{ {k: v for k, v in result.facts.items() if v is not None} }\n\n"
            f"SAMPLE ROWS (partial view, {len(sample)} of "
            f"{result.total_group_count or len(result.rows)}):\n{sample}\n\n"
            f"NOTES: {result.notes or 'none'}\n\n"
            f"Currency symbol: {config.CURRENCY_SYMBOL}"
        )
        resp = Groq(api_key=key).chat.completions.create(
            model=config.ACTIVE_MODEL,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=300,
            reasoning_effort="low",
        )
        text = resp.choices[0].message.content
        text = text.replace(" ", " ").replace("\xa0", " ").strip()

        ok, bad = verify_grounding(text, result.facts, result.rows)
        if not ok:
            # The model produced a figure the database did not. Discard it --
            # a fluent wrong number is worse than a plain right one.
            return fallback, "llm_rejected"
        return normalise_amounts(text, result.facts, result.rows), "llm"
    except Exception:
        return fallback, "template"
