"""
Deterministic Query Layer (Phase 2)
===================================
Every analytical question the assistant can answer, as typed Python functions
that emit parameterised SQL. These become the agent's tools in Phase 4; the
model chooses which to call and with what arguments, and never writes SQL.

Four contracts, each closing a V1 defect by construction rather than by
remembering to do it:

  1. TOTALS ARE ALWAYS COMPUTED (BUGS.md B03/B04)
     Every aggregate result carries `facts` -- a grand total over the FULL
     filtered set, computed with no LIMIT. V1 returned only per-vendor rows and
     left the model to sum 12 numbers in its head, on the single most likely
     demo question.

  2. TRUNCATION IS ALWAYS DISCLOSED (B04)
     `truncated` and `total_group_count` say whether the displayed rows are the
     whole story. V1's silent `LIMIT 20` understated real totals.

  3. DIRECTION IS ALWAYS EXPLICIT (B02)
     `direction` is a required argument. V1 summed Failed payouts into "spend";
     here, mixing credits into a spend total is the same class of error and is
     impossible to do accidentally.

  4. ENTITY SCOPING IS ENFORCED (plan §6.4)
     `entity_id` is required on every query and comes from the session, never
     from the model. The model chooses what to filter, never whose data.

Routing: month-aligned aggregates read the pre-aggregated rollup (~1-2ms at 4M
rows); anything else falls back to the fact table (~8ms). Snapping a
non-aligned window onto month boundaries would be a B01-class wrong answer, so
the router is conservative.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

import config
import db
import security
from db import RESOLVED, ALL_TIME, UNRESOLVED, TimeRange


class UnresolvedFilterError(ValueError):
    """
    A filter was requested but could not be resolved.

    Raised rather than silently dropped: dropping an unparseable time filter is
    exactly the V1 bug that turned a one-month question into an all-time answer
    (BUGS.md B01). The agent catches this and asks the user.
    """

    def __init__(self, field_name: str, value, suggestions=None):
        self.field_name = field_name
        self.value = value
        self.suggestions = suggestions or []
        super().__init__(f"Could not resolve {field_name}: {value!r}")


@dataclass
class QueryResult:
    """
    One answer. `rows` may be truncated for display; `facts` never is.

    The explainer is given `facts` as authoritative and `rows` explicitly
    labelled as a sample, so no narrative statistic can depend on a truncated
    view (BUGS.md B05).
    """
    rows: pd.DataFrame
    facts: dict
    sql: str
    params: list
    source: str                      # 'rollup_monthly' or 'txn_fact'
    truncated: bool = False
    total_group_count: int = 0
    latency_ms: float = 0.0
    filters: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def display_sql(self) -> str:
        """Human-readable SQL for the audit drawer. Never executed."""
        out = self.sql
        for p in self.params:
            lit = f"'{p}'" if isinstance(p, str) else ("NULL" if p is None else str(p))
            out = out.replace("?", lit, 1)
        return out


# =============================================================
# INTERNALS
# =============================================================

def _require_entity(entity_id):
    if not entity_id:
        raise ValueError(
            "entity_id is required. It is supplied by the session, never by the "
            "model -- an unscoped query would read another customer's data."
        )


def _require_direction(direction):
    if direction not in config.VALID_TXN_TYPES:
        raise ValueError(
            f"direction must be one of {config.VALID_TXN_TYPES}, got {direction!r}. "
            f"Aggregating without an explicit direction mixes money in with money out."
        )


def _coerce_range(time_range) -> TimeRange:
    """Accepts a TimeRange, a period string, or None."""
    if isinstance(time_range, TimeRange):
        tr = time_range
    elif time_range is None:
        tr = TimeRange(status=ALL_TIME, label="all time", canonical="all_time")
    else:
        raise TypeError("time_range must be a TimeRange (resolve it first) or None")
    if tr.status == UNRESOLVED:
        raise UnresolvedFilterError("time period", tr.label, tr.suggestions)
    return tr


def _num(v, default=0.0) -> float:
    """
    Coerces a possibly-NULL aggregate to a float.

    An aggregate over an empty filter returns NaN, not None, and float(nan)
    succeeds silently -- which is how a UI ends up rendering 'Rs nan'.
    """
    if v is None or (isinstance(v, float) and v != v) or pd.isna(v):
        return default
    return float(v)


def _use_rollup(tr: TimeRange) -> bool:
    """Aggregates may use the rollup only when the window is provably month-aligned."""
    return tr.status == ALL_TIME or tr.month_aligned


# =============================================================
# SPEND
# =============================================================

def query_spend(con, entity_id: str, direction: str, time_range=None,
                merchant: str = None, kind: str = None,
                group_by_month: bool = False) -> QueryResult:
    """
    Total moved in one direction, optionally for one counterparty.

    Answers "how much have I spent on Swiggy last month" and "how much have I
    spent on Zomato total".
    """
    t0 = time.perf_counter()
    _require_entity(entity_id)
    _require_direction(direction)
    tr = _coerce_range(time_range)
    txn = config.SCHEMA_CONFIG["transaction"]

    use_rollup = _use_rollup(tr)
    src = config.TABLE_ROLLUP_MONTHLY if use_rollup else config.TABLE_TXN_FACT
    amount = "total_amount" if use_rollup else txn["amount_col"]
    count = "txn_count" if use_rollup else "1"
    date_col = "txn_month" if use_rollup else txn["date_col"]
    type_col = "transaction_type" if use_rollup else txn["type_col"]

    where = ["entity_id = ?", f"{type_col} = ?"]
    params = [entity_id, direction]
    if merchant:
        where.append("merchant_norm = ?")
        params.append(merchant)
    if kind:
        where.append("counterparty_kind = ?")
        params.append(kind)
    if tr.status == RESOLVED:
        if use_rollup:
            where.append(f"{date_col} BETWEEN ? AND ?")
            params.extend([tr.start, tr.end])
        else:
            where.append(f"{date_col} >= CAST(? AS DATE)")
            where.append(f"{date_col} < CAST(? AS DATE) + INTERVAL 1 DAY")
            params.extend([tr.start, tr.end])
    clause = " AND ".join(where)

    sum_n = f"SUM({count})" if use_rollup else "COUNT(*)"
    facts_sql = f"""
        SELECT ROUND(SUM({amount}), 2)            AS grand_total,
               {sum_n}                            AS txn_count,
               COUNT(DISTINCT merchant_norm)      AS merchant_count,
               MIN({date_col})                    AS first_seen,
               MAX({date_col})                    AS last_seen
        FROM {src} WHERE {clause}
    """
    f = db.query_df(con, facts_sql, params)
    row = f.iloc[0] if not f.empty else {}
    facts = {
        "grand_total": round(_num(row.get("grand_total")), 2),
        "txn_count": int(_num(row.get("txn_count"))),
        "merchant_count": int(_num(row.get("merchant_count"))),
        "first_seen": None if pd.isna(row.get("first_seen")) else str(row.get("first_seen"))[:10],
        "last_seen": None if pd.isna(row.get("last_seen")) else str(row.get("last_seen"))[:10],
        "direction": direction,
        "period": tr.label,
        "merchant": merchant,
        "counterparty_kind": kind,
    }
    facts["average"] = round(facts["grand_total"] / facts["txn_count"], 2) if facts["txn_count"] else 0.0

    if group_by_month:
        rows_sql = f"""
            SELECT {date_col} AS month, ROUND(SUM({amount}), 2) AS total_amount,
                   {sum_n} AS txn_count
            FROM {src} WHERE {clause} GROUP BY 1 ORDER BY 1
        """
    else:
        rows_sql = f"""
            SELECT merchant_norm, ANY_VALUE(counterparty_kind) AS counterparty_kind,
                   ROUND(SUM({amount}), 2) AS total_amount, {sum_n} AS txn_count
            FROM {src} WHERE {clause} GROUP BY 1 ORDER BY total_amount DESC
        """
    rows = db.query_df(con, rows_sql, params)

    if group_by_month and not rows.empty:
        # The narrator must be HANDED the top month, not left to find it in a
        # sample: given eight of twenty-four rows it confidently named the
        # wrong month (BUGS.md B05 in a new outfit). These facts are computed
        # over every row.
        ranked = rows.sort_values("total_amount", ascending=False)
        top, low = ranked.iloc[0], ranked.iloc[-1]
        facts.update({
            "months": int(len(rows)),
            "top_month": str(top["month"])[:7],
            "top_month_total": round(float(top["total_amount"]), 2),
            "low_month": str(low["month"])[:7],
            "low_month_total": round(float(low["total_amount"]), 2),
            "monthly_average": round(float(rows["total_amount"].mean()), 2),
        })

    notes = []
    if not use_rollup and tr.status == RESOLVED:
        notes.append("Window is not month-aligned; answered from the transaction "
                     "table rather than the monthly rollup.")
    if facts["txn_count"] == 0:
        notes.append("No matching transactions in this window.")

    return QueryResult(
        rows=rows, facts=facts, sql=rows_sql.strip(), params=params, source=src,
        truncated=False, total_group_count=len(rows),
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        filters={"entity_id": entity_id, "merchant": merchant, "direction": direction,
                 "kind": kind, "start": tr.start, "end": tr.end, "period": tr.label},
        notes=notes,
    )


# =============================================================
# RANKING
# =============================================================

def top_counterparties(con, entity_id: str, direction: str, time_range=None,
                       limit: int = 10, kind: str = None,
                       include_excluded_kinds: bool = False) -> QueryResult:
    """
    Ranked counterparties. Answers "which vendor have I spent on the most".

    Bank charges, self-transfers and unattributed rows are excluded by default:
    a fee is not a vendor, and an own-account transfer is not spend. Their value
    is still reported in `facts` so nothing silently disappears.
    """
    t0 = time.perf_counter()
    _require_entity(entity_id)
    _require_direction(direction)
    tr = _coerce_range(time_range)
    txn = config.SCHEMA_CONFIG["transaction"]

    use_rollup = _use_rollup(tr)
    src = config.TABLE_ROLLUP_MONTHLY if use_rollup else config.TABLE_TXN_FACT
    amount = "total_amount" if use_rollup else txn["amount_col"]
    date_col = "txn_month" if use_rollup else txn["date_col"]
    type_col = "transaction_type" if use_rollup else txn["type_col"]
    sum_n = "SUM(txn_count)" if use_rollup else "COUNT(*)"

    where = ["entity_id = ?", f"{type_col} = ?"]
    params = [entity_id, direction]
    if kind:
        where.append("counterparty_kind = ?")
        params.append(kind)
    elif not include_excluded_kinds:
        placeholders = ", ".join("?" for _ in config.KINDS_EXCLUDED_FROM_SPEND_RANKING)
        where.append(f"counterparty_kind NOT IN ({placeholders})")
        params.extend(config.KINDS_EXCLUDED_FROM_SPEND_RANKING)
    if tr.status == RESOLVED:
        if use_rollup:
            where.append(f"{date_col} BETWEEN ? AND ?")
            params.extend([tr.start, tr.end])
        else:
            where.append(f"{date_col} >= CAST(? AS DATE)")
            where.append(f"{date_col} < CAST(? AS DATE) + INTERVAL 1 DAY")
            params.extend([tr.start, tr.end])
    clause = " AND ".join(where)

    # Grand total over ALL groups, not just the displayed top N. This is the
    # B04 fix: the LIMIT applies to presentation only.
    f = db.query_df(con, f"""
        SELECT ROUND(SUM({amount}), 2) AS grand_total, {sum_n} AS txn_count,
               COUNT(DISTINCT merchant_norm) AS group_count,
               MIN({date_col}) AS first_seen, MAX({date_col}) AS last_seen
        FROM {src} WHERE {clause}
    """, params)
    row = f.iloc[0] if not f.empty else {}
    group_count = int(_num(row.get("group_count")))

    rows_sql = f"""
        SELECT merchant_norm, ANY_VALUE(counterparty_kind) AS counterparty_kind,
               ROUND(SUM({amount}), 2) AS total_amount, {sum_n} AS txn_count
        FROM {src} WHERE {clause}
        GROUP BY merchant_norm ORDER BY total_amount DESC LIMIT {int(limit)}
    """
    rows = db.query_df(con, rows_sql, params)

    facts = {
        "grand_total": round(_num(row.get("grand_total")), 2),
        "txn_count": int(_num(row.get("txn_count"))),
        "group_count": group_count,
        "shown": len(rows),
        "direction": direction,
        "period": tr.label,
        "counterparty_kind": kind,
        "top_name": rows.iloc[0]["merchant_norm"] if not rows.empty else None,
        "top_amount": round(float(rows.iloc[0]["total_amount"]), 2) if not rows.empty else 0.0,
    }

    notes = []
    truncated = group_count > len(rows)
    if truncated:
        notes.append(f"Showing top {len(rows)} of {group_count} counterparties. "
                     f"The total above covers all {group_count}.")
    if not kind and not include_excluded_kinds:
        exc = db.query_df(con, f"""
            SELECT ROUND(SUM({amount}), 2) AS excluded_total
            FROM {src}
            WHERE entity_id = ? AND {type_col} = ?
              AND counterparty_kind IN ({", ".join("?" for _ in config.KINDS_EXCLUDED_FROM_SPEND_RANKING)})
        """, [entity_id, direction, *config.KINDS_EXCLUDED_FROM_SPEND_RANKING])
        excluded = round(_num(exc.iloc[0]["excluded_total"]) if not exc.empty else 0.0, 2)
        facts["excluded_total"] = excluded
        if excluded > 0:
            notes.append(f"Excludes {config.CURRENCY_SYMBOL}{excluded:,.2f} of bank "
                         f"charges, self-transfers and unattributed activity.")

    return QueryResult(
        rows=rows, facts=facts, sql=rows_sql.strip(), params=params, source=src,
        truncated=truncated, total_group_count=group_count,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        filters={"entity_id": entity_id, "direction": direction, "kind": kind,
                 "start": tr.start, "end": tr.end, "period": tr.label},
        notes=notes,
    )


# =============================================================
# DRILL-DOWN
# =============================================================

MAX_LIST_ROWS = 200


ORDER_COLUMNS = {"date": "date_col", "amount": "amount_col"}


def list_transactions(con, entity_id: str, direction: str = None, time_range=None,
                      merchant: str = None, kind: str = None,
                      limit: int = 50, order_by: str = "date", ascending: bool = False,
                      min_amount: float = None, max_amount: float = None) -> QueryResult:
    """
    Individual rows. Always reads the fact table; descriptions are redacted.

    `order_by="amount"` with limit 1 answers "what was my highest transaction";
    `ascending=True` the lowest. `min_amount` answers "which transactions were
    over a lakh". These are deterministic, so the rules planner can answer them
    without a model -- the generated-SQL fallback is for shapes not covered.
    """
    t0 = time.perf_counter()
    _require_entity(entity_id)
    tr = _coerce_range(time_range)
    txn = config.SCHEMA_CONFIG["transaction"]
    limit = max(1, min(int(limit), MAX_LIST_ROWS))
    if order_by not in ORDER_COLUMNS:
        raise ValueError(f"order_by must be one of {sorted(ORDER_COLUMNS)}, got {order_by!r}")
    order_col = txn[ORDER_COLUMNS[order_by]]

    where = ["entity_id = ?"]
    params = [entity_id]
    if direction:
        _require_direction(direction)
        where.append(f"{txn['type_col']} = ?")
        params.append(direction)
    if merchant:
        where.append("merchant_norm = ?")
        params.append(merchant)
    if kind:
        where.append("counterparty_kind = ?")
        params.append(kind)
    if tr.status == RESOLVED:
        where.append(f"{txn['date_col']} >= CAST(? AS DATE)")
        where.append(f"{txn['date_col']} < CAST(? AS DATE) + INTERVAL 1 DAY")
        params.extend([tr.start, tr.end])
    if min_amount is not None:
        where.append(f"{txn['amount_col']} >= ?")
        params.append(float(min_amount))
    if max_amount is not None:
        where.append(f"{txn['amount_col']} <= ?")
        params.append(float(max_amount))
    clause = " AND ".join(where)

    f = db.query_df(con, f"""
        SELECT COUNT(*) AS n, ROUND(SUM({txn['amount_col']}), 2) AS grand_total
        FROM {config.TABLE_TXN_FACT} WHERE {clause}
    """, params)
    row = f.iloc[0] if not f.empty else {}
    total_rows = int(_num(row.get("n")))

    rows_sql = f"""
        SELECT {txn['date_col']} AS transaction_date, merchant_norm,
               counterparty_kind, {txn['type_col']} AS transaction_type,
               {txn['amount_col']} AS amount, channel,
               {txn['ref_col']} AS reference_id, {txn['desc_col']} AS description
        FROM {config.TABLE_TXN_FACT} WHERE {clause}
        ORDER BY {order_col} {'ASC' if ascending else 'DESC'}, {txn['date_col']} DESC
        LIMIT {limit}
    """
    rows = db.query_df(con, rows_sql, params)
    if not rows.empty and "description" in rows.columns:
        rows = rows.copy()
        rows["description"] = rows["description"].map(security.redact_for_llm)

    facts = {
        "grand_total": round(_num(row.get("grand_total")), 2),
        "txn_count": total_rows,
        "shown": len(rows),
        "period": tr.label,
        "merchant": merchant,
        "order_by": order_by,
        "ascending": ascending,
        "min_amount": min_amount,
        "max_amount": max_amount,
    }
    # A single-row extreme ("highest transaction") is quoted verbatim so the
    # narrator can name the row rather than only its amount.
    if len(rows) <= 5 and not rows.empty:
        facts["rows"] = [
            {k: (str(v) if not isinstance(v, (int, float)) else v) for k, v in r.items()}
            for r in rows.to_dict(orient="records")]
        if "amount" in rows.columns:
            facts["max_value"] = round(float(rows["amount"].max()), 2)
            facts["min_value"] = round(float(rows["amount"].min()), 2)
    notes = []
    if total_rows > len(rows):
        notes.append(f"Showing {len(rows)} of {total_rows} transactions. "
                     f"The total above covers all {total_rows}.")

    return QueryResult(
        rows=rows, facts=facts, sql=rows_sql.strip(), params=params,
        source=config.TABLE_TXN_FACT, truncated=total_rows > len(rows),
        total_group_count=total_rows,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        filters={"entity_id": entity_id, "merchant": merchant, "direction": direction,
                 "start": tr.start, "end": tr.end, "period": tr.label},
        notes=notes,
    )


# =============================================================
# COMPARISON  (the requirement the brief names by example)
# =============================================================

def compare_periods(con, entity_id: str, direction: str,
                    range_a: TimeRange, range_b: TimeRange,
                    merchant: str = None) -> QueryResult:
    """
    Two windows side by side with the delta computed in SQL.

    "How does that compare to the month before" is named explicitly in the
    problem statement. Keeping the subtraction in the database preserves the
    zero-LLM-arithmetic guarantee.
    """
    t0 = time.perf_counter()

    # Force chronological order. Models routinely fill period_a with the later
    # window, which makes `delta` negative and produces narration that says
    # "X more" and "a 46% decrease" in the same sentence. Ordering here means
    # the delta always reads as later-minus-earlier regardless of arg order.
    if (range_a is not None and range_b is not None
            and range_a.start and range_b.start and range_a.start > range_b.start):
        range_a, range_b = range_b, range_a

    a = query_spend(con, entity_id, direction, range_a, merchant=merchant)
    b = query_spend(con, entity_id, direction, range_b, merchant=merchant)

    ta, tb = a.facts["grand_total"], b.facts["grand_total"]
    delta = round(tb - ta, 2)
    pct = round((delta / ta) * 100, 1) if ta else None

    rows = pd.DataFrame([
        {"period": a.facts["period"], "total_amount": ta, "txn_count": a.facts["txn_count"]},
        {"period": b.facts["period"], "total_amount": tb, "txn_count": b.facts["txn_count"]},
    ])
    facts = {
        "period_a": a.facts["period"], "total_a": ta,
        "period_b": b.facts["period"], "total_b": tb,
        "delta": delta, "pct_change": pct,
        "direction": direction, "merchant": merchant,
        "grand_total": round(ta + tb, 2),
    }
    return QueryResult(
        rows=rows, facts=facts,
        sql=f"-- period A --\n{a.sql}\n\n-- period B --\n{b.sql}",
        params=a.params + b.params, source=f"{a.source}+{b.source}",
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        filters={"entity_id": entity_id, "merchant": merchant, "direction": direction},
        notes=a.notes + b.notes,
    )


# =============================================================
# ACCOUNTS & REFERENCES
# =============================================================

def get_balances(con, entity_id: str, account_id: str = None,
                 all_accounts: bool = False) -> QueryResult:
    """
    Account balances with masked account numbers.

    "What's my balance?" means one account, not a list of ten. By default this
    returns the session's primary account; `all_accounts=True` returns every
    account the customer holds. The combined figure is always reported in
    `facts` so the other accounts are disclosed rather than hidden.
    """
    t0 = time.perf_counter()
    _require_entity(entity_id)
    acct, bank = config.SCHEMA_CONFIG["account"], config.SCHEMA_CONFIG["bank"]

    select = f"""
        SELECT a.{acct['id_col']} AS account_id, a.{acct['number_col']} AS account_number,
               b.{bank['name_col']} AS bank_name, a.{acct['program_col']} AS program_id,
               a.{acct['balance_col']} AS available_balance
        FROM raw_account a LEFT JOIN raw_bank b ON a.{acct['bank_code_col']} = b.{bank['code_col']}
    """
    totals = db.query_df(con, f"""
        SELECT COUNT(*) AS n, ROUND(SUM({acct['balance_col']}), 2) AS combined
        FROM raw_account WHERE {acct['entity_col']} = ?
    """, [entity_id])
    trow = totals.iloc[0] if not totals.empty else {}
    total_accounts = int(_num(trow.get("n")))
    combined = round(_num(trow.get("combined")), 2)

    if account_id and not all_accounts:
        sql = select + f"WHERE a.{acct['entity_col']} = ? AND a.{acct['id_col']} = ?"
        params = [entity_id, account_id]
    else:
        sql = select + (f"WHERE a.{acct['entity_col']} = ? "
                        f"ORDER BY available_balance DESC")
        params = [entity_id]

    rows = db.query_df(con, sql, params)
    if not rows.empty:
        rows = rows.copy()
        rows["account_number"] = rows["account_number"].map(security.mask_account_number)

    shown_total = round(float(rows["available_balance"].sum()), 2) if not rows.empty else 0.0
    primary = rows.iloc[0] if not rows.empty else None
    facts = {
        "grand_total": shown_total,
        "account_count": len(rows),
        "total_accounts": total_accounts,
        "combined_balance": combined,
        # Only meaningful when a single account is shown; attaching the primary
        # account's bank to a multi-account answer invites the model to claim
        # every account is at that bank.
        "bank_name": (primary["bank_name"] if primary is not None and len(rows) == 1
                      else None),
        "account_number": (primary["account_number"]
                           if primary is not None and len(rows) == 1 else None),
    }

    notes = []
    if not all_accounts and account_id and total_accounts > 1:
        notes.append(f"This is your primary account. You hold {total_accounts} accounts "
                     f"in total, with a combined balance of "
                     f"{config.CURRENCY_SYMBOL}{combined:,.2f}.")

    return QueryResult(rows=rows, facts=facts, sql=sql.strip(), params=params,
                       source="raw_account", notes=notes,
                       total_group_count=total_accounts,
                       latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                       filters={"entity_id": entity_id, "account_id": account_id})


def search_reference(con, entity_id: str, reference: str, ref_kind: str = "ref") -> QueryResult:
    """
    Looks up a transaction by reference number.

    Per the schema's guidance a bare "reference number" hits the plaintext
    transaction_reference_id. utr_number is only searched when the user says
    "UTR", and only if a blind-index pepper is configured -- otherwise the
    column is ciphertext we cannot match, and saying so is better than
    returning nothing and implying the reference does not exist.
    """
    t0 = time.perf_counter()
    _require_entity(entity_id)
    txn = config.SCHEMA_CONFIG["transaction"]

    if ref_kind == "utr":
        if not security.utr_search_available():
            raise UnresolvedFilterError(
                "UTR search", reference,
                ["Search by reference number instead (transaction_reference_id)"],
            )
        col, needle = "utr_blind_idx", security.utr_blind_index(reference)
    else:
        col, needle = txn["ref_col"], str(reference).strip()

    sql = f"""
        SELECT {txn['date_col']} AS transaction_date, merchant_norm,
               {txn['type_col']} AS transaction_type, {txn['amount_col']} AS amount,
               {txn['ref_col']} AS reference_id, channel
        FROM {config.TABLE_TXN_FACT}
        WHERE entity_id = ? AND {col} = ? LIMIT 50
    """
    rows = db.query_df(con, sql, [entity_id, needle])
    facts = {"match_count": len(rows), "reference": reference, "searched_column": col,
             "grand_total": round(float(rows["amount"].sum()), 2) if not rows.empty else 0.0}
    return QueryResult(rows=rows, facts=facts, sql=sql.strip(), params=[entity_id, needle],
                       source=config.TABLE_TXN_FACT,
                       latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                       filters={"entity_id": entity_id, "reference": reference})
