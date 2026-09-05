"""
Sandboxed Generated SQL
=======================
The long-tail fallback: when no built-in tool fits, the model may write a
SELECT. This module is what makes that acceptable in a finance system that
otherwise forbids text-to-SQL (ARCHITECTURE_V2.md §14.2).

The model never sees data. It sees SCHEMA_DOC -- two views, their columns and
enums -- and returns SQL with `?` placeholders plus a params list. Every
invariant the rest of the system relies on is enforced HERE, structurally,
rather than requested in a prompt:

  Tenancy     The views `my_transactions` / `my_accounts` are created per
              request, already filtered to the session's entity_id. The model
              cannot name `txn_fact`; the validator rejects it. There is no SQL
              the model can write that reads another customer's rows.

  PII         `utr_number` and raw `account_number` are simply not columns of
              the views. `description` is redacted in the result before it
              reaches narration.

  Injection   Literals arrive as bound parameters. The SQL text itself is
              parsed by DuckDB (`json_serialize_sql`) WITHOUT executing, and
              anything that is not exactly one SELECT over the two views is
              refused -- including table functions such as read_parquet.

  Cost        A LIMIT is enforced on every query.

What this does NOT guarantee is that the model understood the question. That
is why answers produced this way carry a lower confidence band and say so.
"""

import json
import re
import time

import pandas as pd

import config
import security
from queries import QueryResult, _num

VIEW_TXN = "my_transactions"
VIEW_ACC = "my_accounts"
ALLOWED_TABLES = {VIEW_TXN, VIEW_ACC}
ROW_CAP = 200

# Functions that reach outside the query: filesystem, network, settings, or
# anything that could exfiltrate. DuckDB's parser exposes function names, so
# these are refused before execution regardless of how they are spelled.
FORBIDDEN_FUNCTION_PREFIXES = (
    "read_", "glob", "getenv", "current_setting", "load", "install",
    "attach", "detach", "copy", "export", "import", "sniff_csv", "parquet_",
    "duckdb_", "pragma_", "system", "shell", "http", "json_serialize_sql",
    "json_deserialize_sql", "json_execute_serialized_sql",
)

SCHEMA_DOC = f"""You may query exactly two views. Both are already restricted to
the signed-in customer; you cannot and must not filter by entity or account owner.

{VIEW_TXN}  -- one row per transaction, all of this customer's accounts
    transaction_id            VARCHAR
    account_id                VARCHAR      -- joins {VIEW_ACC}.account_id
    transaction_date          TIMESTAMP    -- full date and time
    txn_month                 TIMESTAMP    -- first day of the month, for GROUP BY
    transaction_type          VARCHAR      -- 'debit' (money out) or 'credit' (money in)
    transaction_amount        DECIMAL      -- always positive; use transaction_type for direction
    merchant                  VARCHAR      -- normalised counterparty, UPPERCASE, e.g. 'SWIGGY'
    counterparty_kind         VARCHAR      -- 'merchant' | 'person' | 'bank_charge' | 'self_transfer' | 'unknown'
    channel                   VARCHAR      -- 'UPI' | 'NEFT' | 'IMPS' | 'RTGS' | 'FT' | 'CHARGE' | 'OTHER'
    transaction_reference_id  VARCHAR
    description               VARCHAR      -- raw bank narration

{VIEW_ACC}  -- one row per account this customer holds
    account_id                VARCHAR
    bank_name                 VARCHAR
    program_id                INTEGER
    available_balance         DECIMAL
    account_number_masked     VARCHAR      -- last 4 digits only

Rules:
- ONE SELECT statement. No other statement type, no semicolons.
- Only the two views above. Never reference other tables.
- Put every literal that comes from the user (names, amounts, dates) in `params`
  and use `?` in the SQL, in order. Compare merchant names in UPPERCASE.
- Always include LIMIT (max {ROW_CAP}). For "highest"/"lowest" use ORDER BY ... LIMIT 1.
- Amounts: 1 lakh = 100000, 1 crore = 10000000, 1k = 1000.
- Dates: the most recent transaction is {{anchor}}; resolve "last month" etc. against it.
"""


class SQLRejected(ValueError):
    """The generated SQL failed validation. The message is safe to show."""


def _walk(node, tables, funcs):
    if isinstance(node, dict):
        t = node.get("type")
        if t == "BASE_TABLE":
            tables.add((node.get("table_name") or "").lower())
        elif t == "TABLE_FUNCTION":
            tables.add("<table_function>")
            fn = node.get("function", {})
            funcs.add((fn.get("function_name") or "?").lower())
        elif t == "FUNCTION":
            funcs.add((node.get("function_name") or "").lower())
        for v in node.values():
            _walk(v, tables, funcs)
    elif isinstance(node, list):
        for v in node:
            _walk(v, tables, funcs)


def validate(cur, sql: str, params: list) -> dict:
    """
    Parses without executing and refuses anything outside the sandbox.

    Returns {"has_limit": bool, "tables": set, "functions": set}.
    """
    if not sql or not sql.strip():
        raise SQLRejected("Empty SQL.")
    text = sql.strip().rstrip(";").strip()
    if ";" in text:
        raise SQLRejected("Only a single statement is allowed.")

    try:
        raw = cur.execute("SELECT json_serialize_sql(?)", [text]).fetchone()[0]
        ast = json.loads(raw)
    except Exception as e:
        raise SQLRejected(f"Could not parse SQL: {e}") from e

    if ast.get("error"):
        # DuckDB refuses to serialise non-SELECT statements at all.
        raise SQLRejected(ast.get("error_message") or "Only SELECT statements are allowed.")

    stmts = ast.get("statements") or []
    if len(stmts) != 1:
        raise SQLRejected("Exactly one SELECT statement is allowed.")
    node = stmts[0].get("node", {})
    if node.get("type") not in ("SELECT_NODE", "SET_OPERATION_NODE"):
        raise SQLRejected("Only SELECT statements are allowed.")

    tables, funcs = set(), set()
    _walk(stmts[0], tables, funcs)

    bad_tables = tables - ALLOWED_TABLES
    if bad_tables:
        raise SQLRejected(
            f"Only {VIEW_TXN} and {VIEW_ACC} may be queried; "
            f"found {', '.join(sorted(bad_tables))}.")
    for fn in funcs:
        if fn.startswith(FORBIDDEN_FUNCTION_PREFIXES):
            raise SQLRejected(f"Function '{fn}' is not permitted.")

    placeholders = text.count("?")
    if placeholders != len(params or []):
        raise SQLRejected(
            f"SQL has {placeholders} '?' placeholder(s) but {len(params or [])} "
            f"parameter(s) were supplied.")

    has_limit = any(m.get("type") == "LIMIT_MODIFIER"
                    for m in (node.get("modifiers") or []))
    return {"has_limit": has_limit, "tables": tables, "functions": funcs}


def _quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def create_views(cur, entity_id: str):
    """
    Per-request, entity-scoped TEMP views.

    entity_id comes from the server-side session, never from the model, so
    embedding it (escaped) here is the tenancy boundary made structural: the
    view definition is the only place the filter exists, and the model cannot
    reach past it.
    """
    txn = config.SCHEMA_CONFIG["transaction"]
    acct = config.SCHEMA_CONFIG["account"]
    bank = config.SCHEMA_CONFIG["bank"]
    e = _quote(entity_id)
    keep = config.ACCOUNT_NUMBER_VISIBLE_SUFFIX
    cur.execute(f"""
        CREATE OR REPLACE TEMP VIEW {VIEW_TXN} AS
        SELECT {txn['id_col']}            AS transaction_id,
               {txn['account_id_col']}    AS account_id,
               {txn['date_col']}          AS transaction_date,
               txn_month,
               {txn['type_col']}          AS transaction_type,
               {txn['amount_col']}        AS transaction_amount,
               merchant_norm              AS merchant,
               counterparty_kind,
               channel,
               {txn['ref_col']}           AS transaction_reference_id,
               {txn['desc_col']}          AS description
        FROM {config.TABLE_TXN_FACT}
        WHERE entity_id = {e}
    """)
    cur.execute(f"""
        CREATE OR REPLACE TEMP VIEW {VIEW_ACC} AS
        SELECT a.{acct['id_col']}         AS account_id,
               b.{bank['name_col']}       AS bank_name,
               a.{acct['program_col']}    AS program_id,
               a.{acct['balance_col']}    AS available_balance,
               repeat('X', greatest(length(a.{acct['number_col']}) - {keep}, 0))
                 || right(a.{acct['number_col']}, {keep}) AS account_number_masked
        FROM raw_account a
        LEFT JOIN raw_bank b ON a.{acct['bank_code_col']} = b.{bank['code_col']}
        WHERE a.{acct['entity_col']} = {e}
    """)


def run(con, entity_id: str, sql: str, params: list = None,
        purpose: str = "") -> QueryResult:
    """Validates, scopes, caps and executes model-written SQL."""
    t0 = time.perf_counter()
    params = list(params or [])
    cur = con.cursor()
    try:
        create_views(cur, entity_id)
        info = validate(cur, sql, params)
        text = sql.strip().rstrip(";").strip()

        # A query that already orders and limits keeps its own semantics when
        # wrapped; one without a LIMIT gets ours appended so the cap applies.
        exec_sql = (f"SELECT * FROM ({text}) AS q LIMIT {ROW_CAP}"
                    if info["has_limit"] else f"{text} LIMIT {ROW_CAP}")
        try:
            rows = cur.execute(exec_sql, params).df()
        except Exception as e:
            # A reference to a column that is not in the views (utr_number,
            # account_number) lands here. Same outcome as validation: refused,
            # with a message the agent can act on instead of a raw traceback.
            raise SQLRejected(f"The query could not run against the available "
                              f"columns: {str(e).splitlines()[0][:160]}") from e
    finally:
        cur.close()

    if "description" in rows.columns:
        rows = rows.copy()
        rows["description"] = rows["description"].map(security.redact_for_llm)

    facts = _summarise(rows, purpose)
    return QueryResult(
        rows=rows, facts=facts, sql=text, params=params,
        source="generated_sql", truncated=len(rows) >= ROW_CAP,
        total_group_count=len(rows),
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        filters={"entity_id": entity_id, "via": "generated_sql", "purpose": purpose},
        notes=(["The result was capped at "
                f"{ROW_CAP} rows; add a filter or aggregate for the full picture."]
               if len(rows) >= ROW_CAP else []),
    )


def _summarise(rows: pd.DataFrame, purpose: str) -> dict:
    """
    Facts the narrator may quote. Computed in Python over the full (capped)
    result so the model is never the one doing arithmetic.
    """
    facts = {"row_count": int(len(rows)), "purpose": purpose or None}
    if rows.empty:
        facts["grand_total"] = 0.0
        return facts

    numeric = [c for c in rows.columns
               if pd.api.types.is_numeric_dtype(rows[c])
               and not c.lower().endswith("_id") and c.lower() != "program_id"]
    # Prefer an amount-like column for the headline figure.
    preferred = [c for c in numeric if any(k in c.lower() for k in
                 ("amount", "total", "sum", "balance", "spend", "value"))]
    head = (preferred or numeric or [None])[0]
    if head is not None:
        col = rows[head].dropna()
        facts["headline_column"] = head
        facts["grand_total"] = round(_num(col.sum()), 2)
        facts["max_value"] = round(_num(col.max()), 2)
        facts["min_value"] = round(_num(col.min()), 2)
        if len(col):
            facts["average"] = round(_num(col.mean()), 2)
    else:
        facts["grand_total"] = 0.0

    # Small results are quoted verbatim so a "highest transaction" answer can
    # name the row instead of just its amount.
    if len(rows) <= 5:
        facts["rows"] = [
            {k: (str(v) if not isinstance(v, (int, float)) else v)
             for k, v in r.items()}
            for r in rows.to_dict(orient="records")]
    return facts
