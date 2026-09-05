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
import llm

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


def describe_scope(result) -> str:
    """
    The scope a result actually covers, stated for the narrator.

    Left to infer scope from the user's wording, the model reasons about the
    words instead of the data: after a user dropped the Swiggy filter it
    reported "no transactions labelled Swiggy" because Swiggy was in the
    question and not in the table. Telling it the resolved scope removes the
    inference.
    """
    fl = result.filters or {}
    if fl.get("via") == "generated_sql":
        return (f"A custom query over the customer's own transactions/accounts. "
                f"Purpose: {fl.get('purpose') or 'as asked'}.")
    bits = []
    m = fl.get("merchant")
    bits.append(f"counterparty = {m}" if m else
                "counterparty = ALL (no counterparty filter is applied)")
    if fl.get("period"):
        bits.append(f"period = {fl['period']}")
    elif fl.get("start") and fl.get("end"):
        bits.append(f"period = {fl['start']} to {fl['end']}")
    else:
        bits.append("period = all available history")
    d = fl.get("direction")
    bits.append("direction = money out (debit)" if d == config.TXN_DEBIT else
                "direction = money in (credit)" if d == config.TXN_CREDIT else
                "direction = both in and out")
    if fl.get("kind"):
        bits.append(f"counterparty kind = {fl['kind']}")
    if fl.get("account_id"):
        bits.append("account = the customer's primary account")
    return "; ".join(bits)


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

    if kind == "spend" and result.rows is not None and "month" in result.rows.columns \
            and not result.rows.empty:
        # Monthly breakdown: "on which month did I spend the most". The rows
        # are ordered by month; rank them here so the top month is named.
        r = result.rows.copy()
        r["total_amount"] = r["total_amount"].astype(float)
        top = r.sort_values("total_amount", ascending=False).iloc[0]
        low = r.sort_values("total_amount", ascending=True).iloc[0]
        name = f.get("merchant")
        who = f" with **{name}**" if name else ""
        return (f"Your highest-spending month{who} was **{str(top['month'])[:7]}** at "
                f"**{money(top['total_amount'])}**; the lowest was "
                f"**{str(low['month'])[:7]}** at **{money(low['total_amount'])}**. "
                f"Across all **{len(r)}** months the total is **{money(f['grand_total'])}**.")

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
        who = f" with **{f['merchant']}**" if f.get("merchant") else ""
        floor = f.get("min_amount")
        if result.rows.empty:
            if floor is not None:
                return (f"No transactions{who} of **{money(floor)}** or more in {period}. "
                        f"Every transaction in that scope is below that amount.")
            return f"No transactions found{who} for {period}."

        by_amount = f.get("order_by") == "amount"
        n_shown = len(result.rows)

        # A single-row extreme is an answer about one transaction; name it.
        if by_amount and n_shown == 1 and f.get("rows"):
            row = f["rows"][0]
            which = "lowest" if f.get("ascending") else "highest"
            cp = row.get("merchant_norm") or "?"
            # The largest row is often unattributed narration; say so rather
            # than printing the sentinel as if it were a company.
            cp_txt = ("an **unidentified counterparty**" if cp == config.UNKNOWN_MERCHANT
                      else f"**{cp}**")
            return (f"Your {which} transaction{who} was **{money(row.get('amount', 0))}** "
                    f"— a {row.get('transaction_type', '')} to {cp_txt} "
                    f"on {str(row.get('transaction_date', ''))[:19]} via {row.get('channel', '?')}. "
                    f"That is out of **{f['txn_count']}** transaction(s) in {period}.")

        if floor is not None:
            return (f"**{f['txn_count']}** transaction(s){who} of **{money(floor)}** or more "
                    f"in {period}, totalling **{money(f['grand_total'])}**"
                    + (f"; showing the largest {n_shown}." if result.truncated else "."))

        recency = "most recent" if f.get("order_by", "date") == "date" else (
            "smallest" if f.get("ascending") else "largest")
        if result.truncated:
            return (f"Found **{f['txn_count']}** transaction(s){who} totalling "
                    f"**{money(f['grand_total'])}** in {period}; showing the "
                    f"{recency} {n_shown}.")
        return (f"Found **{f['txn_count']}** transaction(s){who} totalling "
                f"**{money(f['grand_total'])}** in {period}.")

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

    if kind == "sql":
        n = f.get("row_count", 0)
        if n == 0:
            return "No rows matched that query."
        head = f.get("headline_column")
        parts = [f"Found **{n}** row(s)" + (f"; showing the first {len(result.rows)}"
                                           if result.truncated else "") + "."]
        if head:
            parts.append(f"`{head}`: total **{money(f['grand_total'])}**, "
                         f"highest **{money(f.get('max_value', 0))}**, "
                         f"lowest **{money(f.get('min_value', 0))}**.")
        if f.get("rows") and n == 1:
            row = f["rows"][0]
            parts.append("Row: " + ", ".join(f"{k} = {v}" for k, v in row.items()
                                              if k != "description"))
        return " ".join(parts)

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
   Describe exactly the INTERPRETATION scope. If the question mentions a name
   that is NOT in the interpretation, the user chose to drop that filter (for
   example after a clarification) -- describe the wider scope; never claim the
   name is absent from the data.
7. If counterparty_kind is 'person', these are individuals -- call them people,
   never merchants or vendors."""


def generate(user_question: str, kind: str, result, resolution=None) -> tuple:
    """
    Returns (answer_text, method) where method is 'llm', 'llm_rejected' or
    'template'.
    """
    fallback = template_answer(kind, result, resolution)

    if not llm.is_configured():
        return fallback, "template"

    try:
        sample = []
        if result.rows is not None and not result.rows.empty:
            sample = security.redact_records(
                result.rows.head(8).to_dict(orient="records"))

        prompt = (
            f"USER QUESTION: {user_question}\n\n"
            f"INTERPRETATION (the scope these facts describe; authoritative):\n"
            f"{describe_scope(result)}\n\n"
            f"FACTS (authoritative, computed by the database):\n"
            f"{format_facts(result.facts)}\n\n"
            f"SAMPLE ROWS (partial view, {len(sample)} of "
            f"{result.total_group_count or len(result.rows)}):\n{sample}\n\n"
            f"NOTES: {result.notes or 'none'}\n\n"
            f"Currency symbol: {config.CURRENCY_SYMBOL}"
        )
        resp = llm.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=300, reasoning_effort="low")
        text = llm.message_text(resp)
        text = text.replace(" ", " ").replace("\xa0", " ").strip()

        ok, bad = verify_grounding(text, result.facts, result.rows)
        if not ok:
            # The model produced a figure the database did not. Discard it --
            # a fluent wrong number is worse than a plain right one.
            return fallback, "llm_rejected"
        return normalise_amounts(text, result.facts, result.rows), "llm"
    except Exception:
        return fallback, "template"
