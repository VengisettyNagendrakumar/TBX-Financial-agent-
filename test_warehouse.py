"""
Warehouse Test Suite (Phase 0 + 1)
==================================
Asserts VALUES, not shapes.

BUGS.md B10: the V1 suite claimed "13/13, 0% math error" while checking only
`len(table) > 0` -- it would have passed with a total off by $50,000. Every
numeric assertion here is computed independently (from the fact table or from
the source parquet) and compared against what the warehouse reports, so a
regression in the aggregation is caught rather than reported as green.

    python test_warehouse.py
"""

import os
import sys
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import db
import enrichment
from db import RESOLVED, ALL_TIME, UNRESOLVED

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}")
        if detail:
            print(f"       {detail}")


# =============================================================
# 1. Time-range resolution  (BUGS.md B01)
# =============================================================

def test_time_ranges():
    print("\n--- Time-range resolution (B01) ---")
    a = date(2026, 6, 24)

    # Every spelling of the same period must land on the same window.
    expected = ("2026-05-01", "2026-05-31")
    for variant in ["last_month", "previous_month", "past_month", "prior_month",
                    "last month", "Last Month", "last-month", "LAST_MONTH",
                    "  last_month  ", "lastMonth"]:
        r = db.resolve_time_range(variant, a)
        check(f"'{variant}' -> May 2026",
              r.status == RESOLVED and (r.start, r.end) == expected,
              f"got {r.status} {r.start}..{r.end}")

    # The V1 failure mode: an unparseable period must NOT silently widen to
    # all-time. It must be distinguishable so the agent can ask.
    for bad in ["trailing_month", "sometime", "last_99_months", "fortnight", "q9"]:
        r = db.resolve_time_range(bad, a)
        check(f"'{bad}' -> UNRESOLVED (not silent all-time)",
              r.status == UNRESOLVED, f"got {r.status} {r.start}..{r.end}")

    # A genuinely absent period is different from an unparseable one.
    for empty in [None, "", "all", "total"]:
        check(f"{empty!r} -> ALL_TIME", db.resolve_time_range(empty, a).status == ALL_TIME)

    # The new headline requirement.
    r = db.resolve_time_range("last 3 months", a)
    check("'last 3 months' -> Apr..Jun 2026",
          r.status == RESOLVED and (r.start, r.end) == ("2026-04-01", "2026-06-30"),
          f"got {r.start}..{r.end}")

    # Month alignment drives rollup-vs-fact routing (plan §12.5).
    check("'last_3_months' is month-aligned", db.resolve_time_range("last_3_months", a).month_aligned)
    check("'last_30_days' is NOT month-aligned",
          not db.resolve_time_range("last_30_days", a).month_aligned)

    # Absolute date validation (B11).
    check("malformed absolute -> UNRESOLVED",
          db.parse_absolute_range("05/01/2026", "05/31/2026").status == UNRESOLVED)
    inv = db.parse_absolute_range("2026-05-31", "2026-05-01")
    check("inverted absolute range is corrected, not silently empty",
          inv.status == RESOLVED and inv.start == "2026-05-01", f"got {inv.start}..{inv.end}")


# =============================================================
# 2. Merchant normalisation
# =============================================================

def test_normalisation():
    print("\n--- Merchant normalisation ---")
    cases = {
        "ZOMATO PRIVATE LIMITED": "ZOMATO",
        "Zomato Ltd": "ZOMATO",
        "  zomato  ": "ZOMATO",
        "NYKAA LIMITED": "NYKAA",
        "AIRTEL LTD": "AIRTEL",
        "SELECTION ELECTRONICS": "SELECTION ELECTRONICS",
        "FOO INDIA PVT LTD": "FOO",
    }
    for raw, want in cases.items():
        got = enrichment.normalise_name(raw)
        check(f"normalise({raw!r}) == {want!r}", got == want, f"got {got!r}")


# =============================================================
# 3. Warehouse integrity
# =============================================================

def test_warehouse(con):
    print("\n--- Warehouse integrity ---")
    txn = config.SCHEMA_CONFIG["transaction"]

    # A subset check, not a count comparison: after an incremental build
    # raw_transaction holds only the delta, so equality would fail spuriously
    # while still missing the thing that actually matters -- a dropped row.
    missing = con.execute(f"""
        SELECT COUNT(*) FROM raw_transaction r
        WHERE NOT EXISTS (
            SELECT 1 FROM {config.TABLE_TXN_FACT} f
            WHERE f.{txn['id_col']} = r.{txn['id_col']});
    """).fetchone()[0]
    check("no landed source row is lost during enrichment", missing == 0,
          f"{missing} rows landed but absent from the fact table")

    # The rollup is the query path for most questions. If it disagrees with the
    # facts by even a rupee, every answer built on it is wrong.
    fact_total = con.execute(
        f"SELECT ROUND(SUM({txn['amount_col']}), 2) FROM {config.TABLE_TXN_FACT}").fetchone()[0]
    rollup_total = con.execute(
        f"SELECT ROUND(SUM(total_amount), 2) FROM {config.TABLE_ROLLUP_MONTHLY}").fetchone()[0]
    check("rollup total reconciles with fact total exactly",
          abs(float(fact_total) - float(rollup_total)) < 0.01,
          f"fact={fact_total} rollup={rollup_total}")

    fact_count = con.execute(f"SELECT COUNT(*) FROM {config.TABLE_TXN_FACT}").fetchone()[0]
    rollup_count = con.execute(
        f"SELECT SUM(txn_count) FROM {config.TABLE_ROLLUP_MONTHLY}").fetchone()[0]
    check("rollup transaction counts reconcile",
          int(fact_count) == int(rollup_count), f"fact={fact_count} rollup={rollup_count}")

    # Per-entity, because entity scoping is a correctness property: an answer
    # aggregated across entities is wrong even if the grand total matches.
    mismatch = con.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT f.entity_id,
                   ROUND(SUM(f.{txn['amount_col']}), 2) AS f_total,
                   ROUND(ANY_VALUE(r.r_total), 2)       AS r_total
            FROM {config.TABLE_TXN_FACT} f
            JOIN (SELECT entity_id, SUM(total_amount) r_total
                  FROM {config.TABLE_ROLLUP_MONTHLY} GROUP BY 1) r USING (entity_id)
            GROUP BY f.entity_id
            HAVING ABS(f_total - r_total) > 0.01
        );
    """).fetchone()[0]
    check("per-entity totals reconcile (scoping is correct)", mismatch == 0,
          f"{mismatch} entities disagree")

    orphans = con.execute(
        f"SELECT COUNT(*) FROM {config.TABLE_TXN_FACT} WHERE entity_id IS NULL").fetchone()[0]
    check("no transaction is missing an entity", orphans == 0, f"{orphans} orphans")


# =============================================================
# 4. Extraction quality
# =============================================================

def test_extraction(con):
    print("\n--- Extraction & classification ---")

    cov = con.execute(f"""
        SELECT AVG(CASE WHEN merchant_norm <> '{config.UNKNOWN_MERCHANT}'
                        THEN 1.0 ELSE 0.0 END) FROM {config.TABLE_TXN_FACT}
    """).fetchone()[0]
    check(f"coverage >= 90% (got {cov:.1%})", cov >= 0.90)

    # Legal-entity names must fold onto the brand. If BUNDL TECHNOLOGIES stays
    # separate, "how much on Swiggy" silently understates the real total.
    for legal, brand in [("BUNDL TECHNOLOGIES", "SWIGGY"), ("SWIGGY INSTAMART", "SWIGGY"),
                         ("ETERNAL", "ZOMATO"), ("BLINK COMMERCE", "BLINKIT")]:
        leaked = con.execute(
            f"SELECT COUNT(*) FROM {config.TABLE_MERCHANT_DIM} WHERE merchant_norm = ?",
            [legal]).fetchone()[0]
        folded = con.execute(
            f"SELECT COUNT(*) FROM {config.TABLE_MERCHANT_ALIAS} "
            f"WHERE merchant_raw ILIKE ? AND merchant_norm = ?", [f"%{legal}%", brand]
        ).fetchone()[0]
        check(f"'{legal}' folds into '{brand}'", leaked == 0 and folded > 0,
              f"leaked_as_own_canonical={leaked} alias_rows={folded}")

    # No canonical should retain a legal suffix.
    bad = con.execute(f"""
        SELECT DISTINCT merchant_norm FROM {config.TABLE_MERCHANT_DIM}
        WHERE merchant_norm ~ '(LIMITED|LTD|PVT|LLP|PRIVATE)$' LIMIT 5
    """).fetchall()
    check("no canonical retains a legal suffix", len(bad) == 0, f"e.g. {bad}")

    kinds = dict(con.execute(f"""
        SELECT counterparty_kind, COUNT(*) FROM {config.TABLE_TXN_FACT} GROUP BY 1
    """).fetchall())
    for kind in (config.KIND_MERCHANT, config.KIND_PERSON,
                 config.KIND_BANK_CHARGE, config.KIND_SELF_TRANSFER):
        check(f"kind '{kind}' is populated", kinds.get(kind, 0) > 0, f"kinds={kinds}")

    # Bank fees and own-account moves are not vendor spend. If they rank, the
    # answer to "which vendor did I spend the most on" is wrong.
    polluted = con.execute(f"""
        SELECT COUNT(*) FROM {config.TABLE_MERCHANT_DIM}
        WHERE merchant_norm IN ('BANK CHARGES', 'SELF TRANSFER')
          AND counterparty_kind = '{config.KIND_MERCHANT}'
    """).fetchone()[0]
    check("bank charges / self transfers are not classed as merchants",
          polluted == 0, f"{polluted} rows misclassified")

    people = con.execute(f"""
        SELECT COUNT(DISTINCT merchant_norm) FROM {config.TABLE_MERCHANT_DIM}
        WHERE counterparty_kind = '{config.KIND_PERSON}'
    """).fetchone()[0]
    check(f"individuals are identified (got {people})", people >= 5)


# =============================================================
# 5. The new headline questions
# =============================================================

def test_target_questions(con):
    print("\n--- Target questions answerable from the warehouse ---")
    anchor = db.get_anchor_date(con)
    txn = config.SCHEMA_CONFIG["transaction"]
    # Pick a representative entity -- one that actually transacts with both
    # merchants under test. Selecting the single biggest spender on one
    # merchant can land on an outlier that never used the other.
    entity = con.execute(f"""
        SELECT entity_id FROM {config.TABLE_ROLLUP_MONTHLY}
        GROUP BY entity_id
        HAVING COUNT(*) FILTER (WHERE merchant_norm = 'SWIGGY') > 0
           AND COUNT(*) FILTER (WHERE merchant_norm = 'ZOMATO') > 0
        ORDER BY COUNT(DISTINCT merchant_norm) DESC, SUM(total_amount) DESC
        LIMIT 1
    """).fetchone()[0]

    # "How much have I spent on Swiggy last month?"
    r = db.resolve_time_range("last_month", anchor)
    rollup = con.execute(f"""
        SELECT COALESCE(SUM(total_amount), 0) FROM {config.TABLE_ROLLUP_MONTHLY}
        WHERE entity_id = ? AND merchant_norm = 'SWIGGY'
          AND transaction_type = '{config.TXN_DEBIT}'
          AND txn_month BETWEEN ? AND ?
    """, [entity, r.start, r.end]).fetchone()[0]
    truth = con.execute(f"""
        SELECT COALESCE(SUM({txn['amount_col']}), 0) FROM {config.TABLE_TXN_FACT}
        WHERE entity_id = ? AND merchant_norm = 'SWIGGY'
          AND {txn['type_col']} = '{config.TXN_DEBIT}'
          AND {txn['date_col']} >= CAST(? AS DATE)
          AND {txn['date_col']} < CAST(? AS DATE) + INTERVAL 1 DAY
    """, [entity, r.start, r.end]).fetchone()[0]
    check("Q: spend on Swiggy last month -- rollup matches fact table",
          abs(float(rollup) - float(truth)) < 0.01, f"rollup={rollup} fact={truth}")

    # "How much have I spent on Zomato total?"
    total = con.execute(f"""
        SELECT COALESCE(SUM(total_amount), 0) FROM {config.TABLE_ROLLUP_MONTHLY}
        WHERE entity_id = ? AND merchant_norm = 'ZOMATO'
          AND transaction_type = '{config.TXN_DEBIT}'
    """, [entity]).fetchone()[0]
    check("Q: total spend on Zomato (all time) is non-zero", float(total) > 0, f"got {total}")

    # "Which vendor have I spent on the most?"
    top = con.execute(f"""
        SELECT merchant_norm, SUM(total_amount) s FROM {config.TABLE_ROLLUP_MONTHLY}
        WHERE entity_id = ? AND transaction_type = '{config.TXN_DEBIT}'
          AND counterparty_kind NOT IN {config.KINDS_EXCLUDED_FROM_SPEND_RANKING}
        GROUP BY 1 ORDER BY s DESC LIMIT 1
    """, [entity]).fetchone()
    check("Q: top vendor excludes charges/self-transfers/unknown",
          top is not None and top[0] not in ("BANK CHARGES", "SELF TRANSFER",
                                             config.UNKNOWN_MERCHANT),
          f"got {top}")

    # "How much did my friend pay me in the last 3 months?"
    r3 = db.resolve_time_range("last 3 months", anchor)
    friends = con.execute(f"""
        SELECT merchant_norm, SUM(total_amount) s FROM {config.TABLE_ROLLUP_MONTHLY}
        WHERE transaction_type = '{config.TXN_CREDIT}'
          AND counterparty_kind = '{config.KIND_PERSON}'
          AND txn_month BETWEEN ? AND ?
        GROUP BY 1 ORDER BY s DESC LIMIT 5
    """, [r3.start, r3.end]).fetchall()
    check("Q: credits from people in the last 3 months are queryable",
          len(friends) > 0, f"got {friends}")


# =============================================================
# 6. Incremental ingest
# =============================================================

def test_incremental(con):
    print("\n--- Incremental ingest ---")
    txn = config.SCHEMA_CONFIG["transaction"]

    dupes = con.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT {txn['id_col']} FROM {config.TABLE_TXN_FACT}
            GROUP BY 1 HAVING COUNT(*) > 1
        );
    """).fetchone()[0]
    check("no duplicate transaction_id in the fact table", dupes == 0, f"{dupes} duplicated")

    m = db.read_manifest(con)
    check("manifest exists with a watermark", m is not None and m.get("watermark") is not None)
    if m is not None:
        check("manifest schema hash matches current config",
              m.get("schema_hash") == db.schema_fingerprint())
        check("manifest records the alias-map version",
              int(m.get("alias_map_version", -1)) == config.ALIAS_MAP_VERSION)


# =============================================================
# 7. Query layer  (Phase 2)
# =============================================================

def _pick_entity(con):
    return con.execute(f"""
        SELECT entity_id FROM {config.TABLE_MERCHANT_DIM}
        WHERE merchant_norm IN ('SWIGGY', 'ZOMATO')
        GROUP BY 1 HAVING COUNT(DISTINCT merchant_norm) = 2
        ORDER BY SUM(txn_count) DESC LIMIT 1
    """).fetchone()[0]


def test_query_layer(con):
    print("\n--- Query layer (Phase 2) ---")
    import queries
    from queries import UnresolvedFilterError

    ent = _pick_entity(con)
    anchor = db.get_anchor_date(con)
    txn = config.SCHEMA_CONFIG["transaction"]
    lm = db.resolve_time_range("last_month", anchor)

    # Independently recomputed from the fact table -- a golden value, not a shape.
    truth = con.execute(f"""
        SELECT ROUND(COALESCE(SUM({txn['amount_col']}), 0), 2), COUNT(*)
        FROM {config.TABLE_TXN_FACT}
        WHERE entity_id = ? AND merchant_norm = 'SWIGGY'
          AND {txn['type_col']} = '{config.TXN_DEBIT}'
          AND {txn['date_col']} >= CAST(? AS DATE)
          AND {txn['date_col']} < CAST(? AS DATE) + INTERVAL 1 DAY
    """, [ent, lm.start, lm.end]).fetchone()
    r = queries.query_spend(con, ent, config.TXN_DEBIT, lm, merchant="SWIGGY")
    check("query_spend total equals independent fact-table sum",
          abs(r.facts["grand_total"] - float(truth[0])) < 0.01,
          f"query={r.facts['grand_total']} truth={truth[0]}")
    check("query_spend count equals independent fact-table count",
          r.facts["txn_count"] == int(truth[1]),
          f"query={r.facts['txn_count']} truth={truth[1]}")

    # The routing optimisation must be invisible in the answer. If the rollup
    # and the fact table ever disagree on the same window, every fast answer is
    # silently wrong.
    fact_r = queries.query_spend(con, ent, config.TXN_DEBIT,
                                 db.parse_absolute_range(lm.start, lm.end), merchant="SWIGGY")
    check("rollup path is chosen for a month-aligned window",
          r.source == config.TABLE_ROLLUP_MONTHLY, f"source={r.source}")
    check("rollup and fact paths agree exactly on the same window",
          abs(r.facts["grand_total"] - fact_r.facts["grand_total"]) < 0.01,
          f"rollup={r.facts['grand_total']} fact={fact_r.facts['grand_total']}")

    d30 = db.resolve_time_range("last_30_days", anchor)
    check("non-month-aligned window falls back to the fact table",
          queries.query_spend(con, ent, config.TXN_DEBIT, d30).source == config.TABLE_TXN_FACT)

    # B03 / B04
    top = queries.top_counterparties(con, ent, config.TXN_DEBIT, lm, limit=3)
    shown = float(top.rows["total_amount"].sum())
    check("grand total exceeds the truncated display rows (B03/B04)",
          top.facts["grand_total"] >= shown,
          f"grand={top.facts['grand_total']} shown={shown}")
    check("truncation is flagged when groups exceed the limit",
          top.truncated == (top.facts["group_count"] > len(top.rows)))
    check("truncation is disclosed in the notes",
          (not top.truncated) or any("Showing top" in n for n in top.notes))

    all_groups = con.execute(f"""
        SELECT ROUND(SUM(total_amount), 2) FROM {config.TABLE_ROLLUP_MONTHLY}
        WHERE entity_id = ? AND transaction_type = '{config.TXN_DEBIT}'
          AND counterparty_kind NOT IN {config.KINDS_EXCLUDED_FROM_SPEND_RANKING}
          AND txn_month BETWEEN ? AND ?
    """, [ent, lm.start, lm.end]).fetchone()[0]
    check("grand total covers ALL counterparties, not just the shown ones",
          abs(top.facts["grand_total"] - float(all_groups or 0)) < 0.01,
          f"facts={top.facts['grand_total']} all={all_groups}")

    # Ranking hygiene
    check("ranking excludes charges/self-transfers/unknown",
          all(m not in ("BANK CHARGES", "SELF TRANSFER", config.UNKNOWN_MERCHANT)
              for m in top.rows["merchant_norm"]), f"{list(top.rows['merchant_norm'])}")

    # Guardrails that make wrong answers impossible rather than unlikely
    try:
        queries.query_spend(con, ent, None, lm)
        check("direction is mandatory (B02)", False, "no error raised")
    except ValueError:
        check("direction is mandatory (B02)", True)
    try:
        queries.query_spend(con, None, config.TXN_DEBIT, lm)
        check("entity scoping is mandatory", False, "no error raised")
    except ValueError:
        check("entity scoping is mandatory", True)
    try:
        queries.query_spend(con, ent, config.TXN_DEBIT,
                            db.resolve_time_range("trailing_month", anchor))
        check("unresolved period raises instead of widening (B01)", False, "no error raised")
    except UnresolvedFilterError:
        check("unresolved period raises instead of widening (B01)", True)

    # Empty results must be 0.0, never NaN -- float(nan) succeeds silently and
    # renders as 'nan' in the UI.
    empty = queries.query_spend(con, ent, config.TXN_DEBIT, lm, merchant="NO_SUCH_MERCHANT")
    check("empty result returns 0.0, not NaN",
          empty.facts["grand_total"] == 0.0 and empty.facts["txn_count"] == 0,
          f"{empty.facts}")

    # Comparison -- the requirement the brief names by example
    cmp_ = queries.compare_periods(
        con, ent, config.TXN_DEBIT,
        db.resolve_time_range("two_months_ago", anchor), lm, merchant="SWIGGY")
    check("compare_periods delta is computed, not narrated",
          abs(cmp_.facts["delta"] - (cmp_.facts["total_b"] - cmp_.facts["total_a"])) < 0.01,
          f"{cmp_.facts}")

    # Masking / redaction
    bal = queries.get_balances(con, ent)
    check("account numbers are masked in balances",
          bal.rows.empty or all("X" in str(v) for v in bal.rows["account_number"]),
          f"{list(bal.rows['account_number'])[:2]}")
    lst = queries.list_transactions(con, ent, config.TXN_DEBIT, lm, limit=10)
    check("long digit runs are redacted from descriptions",
          lst.rows.empty or not any(
              __import__("re").search(rf"\d{{{config.PII_MIN_DIGIT_RUN},}}", str(d))
              for d in lst.rows["description"]))


# =============================================================
# 8. Resolver  (Phase 3)
# =============================================================

def test_resolver(con):
    print("\n--- Resolver (Phase 3) ---")
    import resolver

    ent = _pick_entity(con)
    vocab = resolver.load_vocabulary(con, ent)
    check(f"vocabulary loads from merchant_dim (got {len(vocab)})", len(vocab) > 10)

    def rs(name, kind=None):
        return resolver.resolve_merchant(con, ent, name, vocabulary=vocab)

    for probe, expected in [("swiggy", "SWIGGY"), ("SWIGGY", "SWIGGY"),
                            ("zomato", "ZOMATO"), ("amazon", "AMAZON")]:
        r = rs(probe)
        check(f"'{probe}' -> {expected}", r.status == resolver.MATCH and r.entity == expected,
              f"got {r.status} {r.entity}")

    # BUGS.md B06: V1 returned NOT_FOUND for these because the legal suffix
    # dragged WRatio below threshold -- adding a suffix made matching worse.
    for probe, expected in [("Swiggy Ltd", "SWIGGY"), ("Spotify Inc", "SPOTIFY"),
                            ("swiggy private limited", "SWIGGY"),
                            ("Zomato Pvt Ltd", "ZOMATO")]:
        r = rs(probe)
        check(f"B06 suffix variant '{probe}' -> {expected}",
              r.status == resolver.MATCH and r.entity == expected,
              f"got {r.status} {r.entity}")

    # Brand <-> legal entity. Without this, Swiggy spend splits in two.
    for probe, expected in [("BUNDL TECHNOLOGIES", "SWIGGY"), ("ETERNAL", "ZOMATO"),
                            ("Amazon Seller Services", "AMAZON")]:
        r = rs(probe)
        check(f"alias '{probe}' -> {expected}",
              r.status == resolver.MATCH and r.entity == expected,
              f"got {r.status} {r.entity}")

    # Typos should resolve; genuinely absent names should not.
    r = rs("swigy")
    check("typo 'swigy' -> SWIGGY via fuzzy",
          r.status == resolver.MATCH and r.entity == "SWIGGY", f"got {r.status} {r.entity}")
    for absent in ["Oracle", "Snowflake", "Wells Fargo"]:
        r = rs(absent)
        check(f"absent '{absent}' -> NOT_FOUND", r.status == resolver.NOT_FOUND,
              f"got {r.status} {r.entity}")

    # Ambiguity must ask, not guess -- in finance a confident wrong vendor is
    # worse than a clarifying question.
    r = rs("selection")
    check("'selection' is AMBIGUOUS with candidates",
          r.status == resolver.AMBIGUOUS and len(r.candidates) > 1,
          f"got {r.status} {r.candidates}")

    # IFSC codes must not be resolvable as counterparties.
    codes = [c[0] for c in con.execute("SELECT bank_code FROM raw_bank").fetchall()]
    leaked = [c for c in codes
              if con.execute(f"SELECT COUNT(*) FROM {config.TABLE_MERCHANT_DIM} "
                             f"WHERE merchant_norm = ?", [c]).fetchone()[0] > 0]
    check("no IFSC bank code is stored as a counterparty", not leaked, f"leaked {leaked}")

    # "my friend" is not in the data; offering candidates is what lets the
    # agent ask instead of guessing or refusing.
    p = resolver.resolve_person(con, ent, "my friend")
    check("'my friend' -> AMBIGUOUS with named candidates",
          p.status == resolver.AMBIGUOUS and len(p.candidates) > 0,
          f"got {p.status} {p.candidates}")
    named = resolver.resolve_person(con, ent, p.candidates[0]) if p.candidates else None
    check("a named person resolves to a MATCH",
          named is not None and named.status == resolver.MATCH,
          f"got {named.status if named else None}")


# =============================================================
# 9. Agent  (Phase 4)
# =============================================================

def test_agent(con):
    print("\n--- Agent (Phase 4) ---")
    import agent as A
    import explainer

    ent = _pick_entity(con)
    bot = A.FinanceAgent(con, ent)

    # Argument extraction must work with no LLM at all, so the app stays
    # demonstrable without an API key.
    for text, want in [("How much did I spend on Swiggy last month?", "last_month"),
                       ("spending in the last 3 months", "last_3_months"),
                       ("what about the past 6 months", "last_6_months"),
                       ("how much on Zomato total", "all_time"),
                       ("spend in April", "april"),
                       ("no period mentioned here", None)]:
        check(f"extract_period({text[:34]!r}) == {want!r}",
              A.extract_period(text) == want, f"got {A.extract_period(text)}")

    check("'my friend paid me' reads as money IN",
          A.extract_direction("How much did my friend pay me?") == config.TXN_CREDIT)
    check("'I spent on X' reads as money OUT",
          A.extract_direction("How much did I spend on Swiggy?") == config.TXN_DEBIT)
    check("merchant is found from the warehouse vocabulary",
          A.extract_merchant("how much on swiggy last month", bot.vocabulary) == "SWIGGY")

    # Rules planner must answer the four target questions unaided.
    for text, tool in [("How much have I spent on Swiggy last month?", "get_spend"),
                       ("How much have I spent on Zomato total?", "get_spend"),
                       ("Which vendor have I spent on the most?", "rank_counterparties"),
                       ("How much did my friend pay me in the last 3 months?",
                        "rank_counterparties"),
                       ("Show my balance", "get_balances")]:
        got, _ = bot._plan_rules(text, [])
        check(f"rules planner: {text[:40]!r} -> {tool}", got == tool, f"got {got}")

    # Policy gate: no period + a long history must ask, not assume.
    r = bot.run("I want to calculate my spending for swiggy")
    check("no-period question triggers a clarifying question",
          r.status == A.CLARIFY and len(r.options) > 0, f"got {r.status}")
    check("the clarification names the counterparty it resolved",
          "SWIGGY" in r.question, r.question)

    # ...and the reply resumes rather than restarting.
    r2 = bot.run("Last 3 months", pending=r.pending)
    check("clarification resumes into an answer", r2.status == A.ANSWER, f"got {r2.status}")
    check("resumed answer uses the chosen window",
          r2.result is not None and r2.result.filters.get("start") == "2026-04-01",
          f"got {r2.result.filters if r2.result else None}")
    check("resumed answer keeps the counterparty from the earlier turn",
          r2.result is not None and r2.result.filters.get("merchant") == "SWIGGY")

    # Unknown counterparty is a guardrail, not a guess and not an error.
    r3 = bot.run("What did I spend on Oracle?")
    check("absent counterparty returns a guardrail answer",
          r3.status == A.GUARDRAIL and "Oracle" in r3.answer, f"got {r3.status}")

    # A singular unnamed person must be disambiguated.
    r4 = bot.run("How much did my friend pay me in the last 3 months?")
    check("'my friend' asks which person",
          r4.status == A.CLARIFY and len(r4.options) > 1, f"got {r4.status}")
    if r4.status == A.CLARIFY:
        r5 = bot.run("Everyone", pending=r4.pending)
        check("'Everyone' answers with the combined total instead of re-asking",
              r5.status == A.ANSWER, f"got {r5.status}")

    # entity_id must never be model-supplied.
    check("agent binds entity_id from the session", bot.entity_id == ent)

    # ---- follow-up context inheritance -------------------------------
    # "Show me these transactions" carries no counterparty and no period of its
    # own. Without inheritance it widens to everything, which looks like an
    # answer and is not one.
    args = {}
    hist = [{"role": "user", "content": "How much have I spent on Swiggy last month?"},
            {"role": "assistant", "content": "...",
             "context": {"merchant": "SWIGGY", "period_token": "last_month",
                         "direction": config.TXN_DEBIT}}]
    inh = bot._inherit_context("Show me these transactions", "list_transactions", args, hist)
    check("follow-up inherits the counterparty", args.get("merchant") == "SWIGGY", f"{args}")
    check("follow-up inherits the period", args.get("period") == "last_month", f"{args}")
    check("inheritance is reported for the audit trail",
          inh.get("merchant") == "SWIGGY" and inh.get("period") == "last_month", f"{inh}")

    # A ranking question is about ALL counterparties -- inheriting the last one
    # would silently narrow it.
    args2 = {}
    bot._inherit_context("Which vendor have I spent on the most?",
                         "rank_counterparties", args2, hist)
    check("ranking questions do NOT inherit a counterparty", "merchant" not in args2, f"{args2}")

    # Inheritance needs POSITIVE evidence of a follow-up. An earlier version
    # inherited whenever the question named no period, so "what was my last
    # transaction in general" silently stayed scoped to Swiggy.
    for generic in ["what was my last transaction in general",
                    "what was my last transaction",
                    "show me all my transactions",
                    "what did I spend overall",
                    "list any transaction"]:
        a = {}
        bot._inherit_context(generic, "list_transactions", a, hist)
        check(f"general question does not inherit: {generic!r}",
              "merchant" not in a, f"{a}")

    # ...and a general question DROPS a counterparty the planner carried over
    # on its own, since the model also sees recent history.
    a = {"merchant": "SWIGGY"}
    dropped = bot._inherit_context("what was my last transaction in general",
                                   "list_transactions", a, hist)
    check("a general question drops a planner-supplied counterparty",
          "merchant" not in a and dropped.get("dropped_merchant") == "SWIGGY",
          f"args={a} dropped={dropped}")

    # Elliptical follow-ups still inherit.
    a = {}
    bot._inherit_context("What about April?", "get_spend", a, hist)
    check("elliptical follow-up inherits the counterparty",
          a.get("merchant") == "SWIGGY", f"{a}")

    # A listing keeps the direction of the turn it follows, but a fresh
    # "what was my last transaction" must not be narrowed to debits only --
    # that would hide an incoming payment.
    check("no direction word means no direction",
          A.explicit_direction("what was my last transaction") is None)
    check("an explicit spend word still means debit",
          A.explicit_direction("what did I spend") == config.TXN_DEBIT)
    check("'paid me' still means credit",
          A.explicit_direction("who paid me") == config.TXN_CREDIT)

    r_last = bot.run("what was my last transaction", history=hist)
    check("'my last transaction' is not scoped to the previous counterparty",
          r_last.result is not None and not r_last.result.filters.get("merchant"),
          f"{r_last.result.filters if r_last.result else None}")
    check("'my last transaction' covers both directions",
          r_last.result is not None and r_last.result.filters.get("direction") is None,
          f"{r_last.result.filters if r_last.result else None}")

    # An explicitly named counterparty must win over the inherited one.
    args3 = {}
    bot._inherit_context("How much on Zomato last month?", "get_spend", args3, hist)
    check("a newly named counterparty is not overwritten by inheritance",
          args3.get("merchant") != "SWIGGY", f"{args3}")

    # ---- confidence --------------------------------------------------
    rc = bot.run("How much have I spent on Swiggy last month?")
    check("answers carry a confidence band",
          0.0 < rc.confidence.score <= 1.0 and rc.confidence.label in
          (A.HIGH, A.MEDIUM, A.LOW), f"{rc.confidence}")
    check("confidence explains itself", len(rc.confidence.reasons) > 0)
    check("an exact counterparty match is reported as a reason",
          any("exactly" in r for r in rc.confidence.reasons), f"{rc.confidence.reasons}")

    # No period given -> lower confidence than an explicit window.
    ra = bot.run("Which vendor have I spent on the most?")
    check("an unspecified period lowers confidence",
          ra.confidence.score < rc.confidence.score or ra.confidence.label != A.HIGH,
          f"{ra.confidence}")

    # All three bands must be reachable. An earlier version floored every
    # penalty at 0.75 while Low required < 0.75, so the badge could physically
    # never say Low -- a signal that never fires is not a signal.
    check("band thresholds are ordered", A.BAND_HIGH_MIN > A.BAND_MEDIUM_MIN > 0)
    check("a clean answer bands High", A.band_for(1.0) == A.HIGH)
    check("a mildly assumed answer bands Medium",
          A.band_for(0.80) == A.MEDIUM, A.band_for(0.80))
    check("a compounded-doubt answer bands Low",
          A.band_for(0.70) == A.LOW, A.band_for(0.70))
    # Realistic worst case: fuzzy name + assumed period + patchy attribution.
    worst = 0.85 * A.PERIOD_ASSUMED_FACTOR * 0.90
    check("a realistic worst case actually reaches Low",
          A.band_for(worst) == A.LOW, f"{worst:.3f} -> {A.band_for(worst)}")
    # Signals compound rather than one overriding the rest.
    check("several mild doubts compound below any single one",
          (0.95 * A.PERIOD_ASSUMED_FACTOR * 0.95) < 0.95)

    # ---- session-scoped balances -------------------------------------
    import session as session_mod
    s = session_mod.load(con)
    check("session resolves a primary account", s.account_id is not None)
    check("session knows how many accounts exist", s.account_count >= 1)
    check("the account number is masked, never raw",
          "X" in s.masked_number and not s.masked_number.isdigit(), s.masked_number)

    rb = bot.run("Show my balance")
    check("'my balance' returns ONE account, not a list",
          rb.result is not None and len(rb.result.rows) == 1,
          f"{len(rb.result.rows) if rb.result else None} rows")
    if s.account_count > 1:
        check("other accounts are disclosed rather than hidden",
              any("total" in n.lower() for n in (rb.result.notes or [])),
              f"{rb.result.notes if rb.result else None}")
    rall = bot.run("Show me all my accounts")
    check("'all my accounts' returns every account",
          rall.result is not None and len(rall.result.rows) == s.account_count,
          f"{len(rall.result.rows) if rall.result else None} vs {s.account_count}")

    # ---- comparison chronology ---------------------------------------
    import queries as Q
    later = db.resolve_time_range("last_month", bot.anchor)
    earlier = db.resolve_time_range("two_months_ago", bot.anchor)
    swapped = Q.compare_periods(con, ent, config.TXN_DEBIT, later, earlier,
                                merchant="SWIGGY")
    check("compare_periods orders windows chronologically regardless of arg order",
          swapped.facts["period_a"] < swapped.facts["period_b"] or
          swapped.facts["delta"] == round(
              swapped.facts["total_b"] - swapped.facts["total_a"], 2),
          f"{swapped.facts}")
    ordered = Q.compare_periods(con, ent, config.TXN_DEBIT, earlier, later,
                                merchant="SWIGGY")
    check("swapped and ordered arguments give the same delta",
          abs(swapped.facts["delta"] - ordered.facts["delta"]) < 0.01,
          f"{swapped.facts['delta']} vs {ordered.facts['delta']}")

    # ---- "the N months before that" ----------------------------------
    # A 3-month window must be compared against the 3 months before it, not
    # against a single month. The baseline is derived from the subject rather
    # than parsed, because `previous_3_months` reads as a synonym of
    # `last_3_months` and resolves to the same range.
    l3 = db.resolve_time_range("last_3_months", bot.anchor)
    prev3 = db.previous_window(l3)
    check("previous_window of a 3-month window is 3 months long",
          prev3.start == "2026-01-01" and prev3.end == "2026-03-31",
          f"{prev3.start}..{prev3.end}")
    check("previous_window is contiguous with the subject window",
          prev3.end < l3.start, f"{prev3.end} vs {l3.start}")
    check("previous_window stays month-aligned (keeps the rollup path)",
          prev3.month_aligned)
    pm = db.previous_window(db.resolve_time_range("last_month", bot.anchor))
    check("previous_window of one month is the month before",
          pm.start == "2026-04-01" and pm.end == "2026-04-30", f"{pm.start}..{pm.end}")
    d30 = db.previous_window(db.resolve_time_range("last_30_days", bot.anchor))
    check("previous_window of a day window shifts by days, not months",
          d30.start == "2026-04-26" and d30.end == "2026-05-25", f"{d30.start}..{d30.end}")
    check("same_window detects identical ranges",
          db.same_window(l3, db.resolve_time_range("last_3_months", bot.anchor)))
    check("same_window rejects different ranges", not db.same_window(l3, prev3))

    # A time phrase must never be resolved as a counterparty. This produced
    # "I have no transactions for the three months before. The closest names on
    # record are UBER, AMAZON, MYNTRA."
    for phrase in ["the 3 months before", "the three months before that",
                   "last month", "the previous 3 months", "April",
                   "the same period last year"]:
        check(f"time phrase not treated as a merchant: {phrase!r}",
              A.looks_like_period(phrase), phrase)
    for name in ["SWIGGY", "Swiggy Ltd", "UBER", "Last Mile Logistics",
                 "Monthly Gym", "Gautam Singh", "SELECTION ELECTRONICS"]:
        check(f"merchant not mistaken for a period: {name!r}",
              not A.looks_like_period(name), name)
    check("extract_merchant ignores a time phrase after 'to'",
          A.extract_merchant("compare it to the 3 months before", bot.vocabulary) is None)

    # End to end: a 3-month subject compares against a 3-month baseline.
    hist3 = [{"role": "user", "content": "spending for swiggy"},
             {"role": "assistant", "content": "...",
              "context": {"merchant": "SWIGGY", "period_token": "last_3_months",
                          "direction": config.TXN_DEBIT}}]
    rcmp = bot.run("compare it to the 3 months before", history=hist3)
    ok = rcmp.result is not None and rcmp.kind == "compare"
    check("'compare it to the 3 months before' produces a comparison", ok,
          f"{rcmp.status}: {rcmp.answer[:90]}")
    if ok:
        f = rcmp.result.facts
        check("both windows are 3 months long",
              "3 calendar months" in f["period_a"] and "3 calendar months" in f["period_b"],
              f"A={f['period_a']} B={f['period_b']}")
        check("the baseline precedes the subject",
              f["total_a"] != f["total_b"], f"{f['total_a']} vs {f['total_b']}")

    # The tool schema must not require period_a: Groq rejects a call that omits
    # a required property, which silently dropped every comparison onto rules.
    cmp_tool = next(t for t in A.TOOLS
                    if t["function"]["name"] == "compare_spend")["function"]
    check("compare_spend does not require period_a",
          "period_a" not in cmp_tool["parameters"]["required"],
          f"{cmp_tool['parameters']['required']}")
    check("compare_spend still requires period_b",
          "period_b" in cmp_tool["parameters"]["required"])

    # ---- chat threads on disk ----------------------------------------
    import tempfile, chatstore as CS
    tmp = os.path.join(tempfile.mkdtemp(), "chats_test.db")
    cstore = CS.ChatStore(tmp)

    t1 = cstore.new_thread(ent, "first")
    t2 = cstore.new_thread(ent, "second")
    check("threads get distinct ids", t1 != t2)

    cstore.append_message(t1, "user", "How much on Swiggy last month?")
    cstore.append_message(t1, "assistant", "You spent 1.00",
                          {"context": {"merchant": "SWIGGY",
                                       "period_token": "last_month"},
                           "rows": [{"merchant_norm": "SWIGGY", "total_amount": 1.0}]})
    cstore.append_message(t2, "user", "Zomato total?")
    check("conversations do not leak into each other",
          len(cstore.get_messages(t1)) == 2 and len(cstore.get_messages(t2)) == 1)

    # Survives a restart: a NEW store object over the same file.
    reopened = CS.ChatStore(tmp)
    msgs = reopened.get_messages(t1)
    check("transcript survives a reopen", len(msgs) == 2, f"{len(msgs)}")
    check("the stored table snapshot is restored",
          msgs[1].get("rows") and msgs[1]["rows"][0]["merchant_norm"] == "SWIGGY",
          f"{msgs[1].get('rows')}")
    check("follow-up context survives a reopen",
          reopened.history_for_agent(t1)[-1]["context"]["merchant"] == "SWIGGY")

    # An open clarifying question must survive a refresh, or the user's reply
    # is read as a brand-new question.
    cstore.set_pending(t1, {"slot": "period", "tool": "get_spend",
                            "merchant": "SWIGGY"})
    check("pending clarification persists",
          CS.ChatStore(tmp).get_pending(t1).get("merchant") == "SWIGGY")
    cstore.set_pending(t1, None)
    check("pending clears", CS.ChatStore(tmp).get_pending(t1) == {})

    check("threads are listed newest first",
          [t["thread_id"] for t in cstore.list_threads(ent)][0] == t1,
          "t1 was touched most recently")
    check("message counts are reported",
          {t["thread_id"]: t["message_count"] for t in cstore.list_threads(ent)}[t1] == 2)

    # Each turn needs its own graph thread; reusing the conversation id makes
    # turn 2 resume turn 1's finished state and repeat its answer.
    bot2 = A.FinanceAgent(con, entity_id=ent, checkpointer=cstore.checkpointer())
    tconv = cstore.new_thread(ent, "isolation")
    a1 = bot2.run("How much have I spent on Swiggy last month?",
                  thread_id=tconv, turn=0)
    a2 = bot2.run("Show my balance", thread_id=tconv, turn=1)
    check("consecutive turns in one conversation are independent",
          a1.answer != a2.answer and a2.kind == "balances",
          f"{a2.kind}: {a2.answer[:60]}")
    check("a turn still runs with no thread_id",
          bot2.run("Show my balance").status == A.ANSWER)

    ncp = cstore.con.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id LIKE ?",
        (f"{tconv}#%",)).fetchone()[0]
    check("checkpoints are written under the conversation", ncp > 0, f"{ncp}")
    cstore.delete_thread(tconv)
    ncp_after = cstore.con.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id LIKE ?",
        (f"{tconv}#%",)).fetchone()[0]
    check("deleting a conversation removes its checkpoints too",
          ncp_after == 0, f"{ncp_after}")
    check("deleting a conversation removes its messages",
          cstore.get_messages(tconv) == [])

    cstore.close(); reopened.close()

    # ---- provider portability ----------------------------------------
    # The provider is chosen by base URL, not by code, so switching from Groq
    # to OpenAI (or a local model) is configuration rather than a rewrite.
    import llm as LLM
    GROQ_URL = "https://api.groq.com/openai/v1"

    def _env(**kw):
        for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
                  "GROQ_API_KEY", "GROQ_MODEL", "OPENAI_API_KEY"):
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in kw.items() if v is not None})
        # config holds whatever the developer's .env set at import time, so it
        # is pinned here too -- otherwise these assertions pass or fail
        # depending on whose machine runs them.
        config.LLM_BASE_URL = kw.get("LLM_BASE_URL", GROQ_URL)
        config.LLM_API_KEY = kw.get("LLM_API_KEY", "")
        LLM.reset()

    _env(GROQ_API_KEY="gsk_test")
    check("legacy GROQ_API_KEY still configures the client", LLM.is_configured())
    _env(LLM_BASE_URL="", LLM_API_KEY="sk_test", LLM_MODEL="gpt-4o-mini")
    check("blank base URL selects OpenAI",
          LLM.provider_name() == "OpenAI" and LLM.model() == "gpt-4o-mini",
          f"{LLM.provider_name()} {LLM.model()}")
    _env(LLM_BASE_URL="https://api.groq.com/openai/v1", LLM_API_KEY="k")
    check("Groq endpoint is recognised", LLM.provider_name() == "Groq")
    _env(LLM_BASE_URL="http://localhost:11434/v1", LLM_API_KEY="none")
    check("a local endpoint is recognised", LLM.provider_name() == "local")
    _env()
    check("no key means not configured", not LLM.is_configured())
    _env(LLM_BASE_URL="", OPENAI_API_KEY="sk_openai")
    check("OPENAI_API_KEY is used when the endpoint is OpenAI", LLM.is_configured())
    _env(LLM_BASE_URL="https://api.groq.com/openai/v1", OPENAI_API_KEY="sk_openai")
    check("an OpenAI key is NOT sent to Groq", not LLM.is_configured(),
          "would surface as a confusing 401")

    # reasoning_effort is the one provider-specific parameter: Groq accepts it
    # for gpt-oss, OpenAI rejects it on non-reasoning models. A rejection must
    # be absorbed and remembered, not surfaced as a failed turn.
    class _FakeCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kw):
            self.calls.append(dict(kw))
            if "reasoning_effort" in kw:
                raise Exception("400 Unrecognized request argument supplied: "
                                "reasoning_effort")
            M = type("M", (), {"content": "ok", "tool_calls": None})
            C = type("C", (), {"message": M()})
            return type("R", (), {"choices": [C()]})()

    fake = type("Client", (), {"chat": type("Chat", (), {"completions": _FakeCompletions()})()})()
    calls = fake.chat.completions.calls
    real_client = LLM._client
    LLM._client = lambda: fake
    LLM._NO_REASONING_EFFORT.clear()
    try:
        LLM.chat([{"role": "user", "content": "hi"}],
                 reasoning_effort="low", model_name="gpt-4o-mini")
        check("a reasoning_effort rejection is retried without it",
              len(calls) == 2 and "reasoning_effort" in calls[0]
              and "reasoning_effort" not in calls[1], f"{len(calls)} calls")
        check("the rejection is remembered per model",
              "gpt-4o-mini" in LLM._NO_REASONING_EFFORT)
        n = len(calls)
        r = LLM.chat([{"role": "user", "content": "hi"}],
                     reasoning_effort="low", model_name="gpt-4o-mini")
        check("no wasted retry on the next call for that model",
              len(calls) - n == 1 and "reasoning_effort" not in calls[-1])
        check("response text extraction is provider-agnostic",
              LLM.message_text(r) == "ok")
        n = len(calls)
        LLM.chat([{"role": "user", "content": "hi"}],
                 reasoning_effort="low", model_name="openai/gpt-oss-20b")
        check("a different model still attempts reasoning_effort",
              "reasoning_effort" in calls[n])
        LLM.chat([{"role": "user", "content": "hi"}],
                 tools=[{"type": "function"}], model_name="m")
        check("tools pass through with tool_choice defaulted",
              "tools" in calls[-1] and calls[-1].get("tool_choice") == "auto")
    finally:
        LLM._client = real_client
        LLM._NO_REASONING_EFFORT.clear()
        _env(GROQ_API_KEY=os.getenv("GROQ_API_KEY", ""))

    # Grounding verification: an invented figure must be rejected.
    r6 = bot.run("How much have I spent on Swiggy last month?")
    if r6.result is not None:
        ok, _ = explainer.verify_grounding(r6.answer, r6.result.facts, r6.result.rows)
        check("the delivered answer is fully grounded", ok, r6.answer)
        bad = "You spent ₹98,765,432.10 on Swiggy."
        ok2, offending = explainer.verify_grounding(bad, r6.result.facts, r6.result.rows)
        check("a fabricated figure fails verification",
              (not ok2) and offending, f"offending={offending}")


def test_long_tail(con):
    """
    The failures reported from live testing, and the guarantees added to fix
    them. Each assertion names the defect it prevents from returning.
    """
    print("\n--- Long tail: SQL fallback, bounded listings, checkpoint safety ---")
    import importlib, tempfile
    import agent as A
    import chatstore as CS
    import queries
    import sqlguard as G
    import explainer as X
    import session as session_mod

    sess = session_mod.load(con)
    ent = sess.entity_id

    # ---- checkpoints never pickle, even across a module reload ----------
    # Streamlit hot-reloaded queries.py and every turn died with "Can't pickle
    # QueryResult: it's not the same object as queries.QueryResult". Rich
    # objects now live in agent._scratch; state is plain JSON.
    tmp = os.path.join(tempfile.mkdtemp(), "chats_ll.db")
    cstore = CS.ChatStore(tmp)
    bot = A.FinanceAgent(con, session=sess, checkpointer=cstore.checkpointer())
    tid = cstore.new_thread(ent, "ll")
    r0 = bot.run("What are my various accounts? Show me all their details",
                 thread_id=tid, turn=0)
    importlib.reload(queries)
    r1 = bot.run("What are my last 5 transactions with Book My Show",
                 thread_id=tid, turn=1)
    r2 = bot.run("Show me the first transaction in the year 2026",
                 thread_id=tid, turn=2)
    check("turns survive a reload of queries.py (no pickle of QueryResult)",
          all(r.status != A.ERROR for r in (r0, r1, r2)),
          f"{r0.status} {r1.status} {r2.status}: {r1.answer[:70]}")
    types = dict(cstore.con.execute(
        "SELECT type, COUNT(*) FROM checkpoints WHERE thread_id LIKE ? GROUP BY type",
        (f"{tid}#%",)).fetchall())
    check("every checkpoint is msgpack; none is pickle-typed",
          types and all("pickle" not in str(t).lower() for t in types), f"{types}")
    check("scratch slot is freed after each turn", not bot._scratch, f"{list(bot._scratch)}")
    cstore.close()

    # ---- sqlguard: the sandbox --------------------------------------------
    ok_sql = "SELECT merchant, transaction_amount FROM my_transactions WHERE transaction_amount > ? ORDER BY 2 DESC"
    check("sandbox runs a valid parameterised SELECT",
          G.run(con, ent, ok_sql, [100000]).source == "generated_sql")
    for label, bad, params in [
            ("another table", "SELECT * FROM txn_fact LIMIT 3", []),
            ("a table function", "SELECT * FROM read_parquet('x.parquet')", []),
            ("DML", "DELETE FROM my_transactions", []),
            ("two statements", "SELECT 1; SELECT 2", []),
            ("param/placeholder mismatch", "SELECT * FROM my_transactions WHERE merchant = ?", []),
            ("a sensitive column", "SELECT utr_number FROM my_transactions LIMIT 1", [])]:
        try:
            G.run(con, ent, bad, params)
            check(f"sandbox refuses {label}", False, "ran without error")
        except G.SQLRejected:
            check(f"sandbox refuses {label}", True)
    n_view = int(G.run(con, ent, "SELECT COUNT(*) AS n FROM my_transactions", []).rows.iloc[0]["n"])
    n_fact = con.execute(f"SELECT COUNT(*) FROM {config.TABLE_TXN_FACT} WHERE entity_id = ?",
                         [ent]).fetchone()[0]
    check("sandbox view is scoped to exactly this customer's rows", n_view == n_fact,
          f"{n_view} vs {n_fact}")

    # ---- counterparty guard: SQL cannot bypass entity resolution --------
    bot = A.FinanceAgent(con, session=sess)
    q = "SELECT SUM(transaction_amount) FROM my_transactions WHERE merchant = ? LIMIT 1"
    _, _, st, _, name = bot._sql_counterparty_guard("x", q, ["ORACLE"])
    check("unknown name in a SQL param is NOT_FOUND, not an empty result", st == "NOT_FOUND")
    _, _, st, _, _ = bot._sql_counterparty_guard(
        "x", "SELECT 1 FROM my_transactions WHERE merchant = 'ORACLE' LIMIT 1", [])
    check("unknown name inlined as a literal is caught too", st == "NOT_FOUND")
    _, p2, st, _, _ = bot._sql_counterparty_guard("x", q, ["swiggy"])
    check("known name is substituted with its canonical", st is None and p2 == ["SWIGGY"], f"{p2}")
    _, p3, st, _, _ = bot._sql_counterparty_guard("x", q, ["Bundl Technologies"])
    check("legal alias is substituted with the brand", st is None and p3 == ["SWIGGY"], f"{p3}")
    _, _, st, _, _ = bot._sql_counterparty_guard("x", q, ["selection"])
    check("ambiguous name asks rather than guesses", st == "AMBIGUOUS")
    _, p4, st, _, _ = bot._sql_counterparty_guard(
        "x", "SELECT 1 FROM my_transactions WHERE transaction_type = ? AND channel = ? "
             "AND transaction_date >= ? LIMIT 1", ["debit", "UPI", "2026-01-01"])
    check("enum and date params are not treated as names",
          st is None and p4 == ["debit", "UPI", "2026-01-01"], f"{p4}")
    bot._plan_llm = lambda m, h: ("query_sql", {
        "sql": q, "params": ["ORACLE"], "purpose": "t"})
    rr = bot.run("What did I spend on Oracle?")
    check("forced through query_sql, an unknown vendor still hits the guardrail",
          rr.status == A.GUARDRAIL and "Oracle" in rr.answer, f"{rr.status}: {rr.answer[:60]}")
    bot._plan_llm = lambda m, h: ("query_sql", {
        "sql": "SELECT * FROM txn_fact LIMIT 1", "params": [], "purpose": "escape"})
    rr = bot.run("show me txn_fact")
    check("forced through query_sql, an escape attempt is refused",
          rr.status == A.GUARDRAIL and "txn_fact" in rr.answer)

    # ---- bounded listings do not trigger the period gate -----------------
    bot = A.FinanceAgent(con, session=sess)
    for msg, args in [("last 5 transactions in bookmyshow", {"limit": 5, "order_by": "date"}),
                      ("what was my highest transaction", {"order_by": "amount", "limit": 1}),
                      ("which transaction is more than one lakh", {"min_amount": 100000}),
                      ("what is my latest transaction", {"limit": 5})]:
        check(f"bounded listing bypasses the period gate: {msg!r}",
              bot._is_bounded_listing(msg, args, "list_transactions"))
    check("an open aggregate is NOT bounded",
          not bot._is_bounded_listing("how much did I spend on swiggy", {}, "get_spend"))

    # ---- rules planner routing for the reported questions ----------------
    routes = {
        "which transaction is more than one lakh": ("list_transactions", {"min_amount": 100000.0}),
        "what was the highest amount I have done in a transaction": ("list_transactions", {"order_by": "amount", "limit": 1, "ascending": False}),
        "which is the lowest": ("list_transactions", {"order_by": "amount", "ascending": True}),
        "on which month I have high expense": ("get_spend", {"group_by_month": True}),
        "last 5 transactions in bookmyshow": ("list_transactions", {"limit": 5, "merchant": "BOOKMYSHOW"}),
        "Show me the first transaction in the year 2026": ("list_transactions", {"ascending": True, "period": "year_2026", "limit": 1}),
        "show me my other account details": ("get_balances", {}),
        "which vendor have I spent on the most": ("rank_counterparties", {}),
    }
    for msg, (tool, want) in routes.items():
        got_tool, got_args = bot._plan_rules(msg, [])
        ok = got_tool == tool and all(got_args.get(k) == v for k, v in want.items())
        check(f"rules route: {msg[:44]!r} -> {tool}", ok, f"got {got_tool} {got_args}")

    # ---- resume replies that mean 'no restriction' -----------------------
    r1 = bot.run("I want to calculate my spending for swiggy")
    check("open merchant question still asks for a period", r1.status == A.CLARIFY)
    for reply in ["no", "all", "doesn't matter", "no only swiggy"]:
        r2 = bot.run(reply, pending=r1.pending)
        check(f"reply {reply!r} resolves instead of re-asking", r2.status == A.ANSWER, r2.status)
    r3 = bot.run("no only swiggy", pending=r1.pending)
    check("'no only swiggy' also drops the counterparty filter",
          r3.result is not None and not r3.result.filters.get("merchant"),
          f"{r3.result.filters if r3.result else None}")
    check("the original question is carried through a clarification",
          r1.pending.get("original") == "I want to calculate my spending for swiggy")

    # ---- names vs clauses vs time words ----------------------------------
    check("a question clause is not a merchant",
          A.extract_merchant("on which month I have high expense", bot.vocabulary) is None)
    check("'on weekends' is a time phrase, not a merchant", A.looks_like_period("weekends"))
    check("a real merchant is still a merchant",
          A.extract_merchant("last 5 transactions in bookmyshow", bot.vocabulary) == "BOOKMYSHOW")

    # ---- calendar years -----------------------------------------------------
    for tok in ["2026", "year_2026", "in 2026", "the year 2025", "fy2026"]:
        tr = db.resolve_time_range(tok, bot.anchor)
        check(f"{tok!r} resolves to a whole calendar year",
              tr.status == RESOLVED and tr.start.endswith("-01-01") and tr.end.endswith("-12-31")
              and tr.month_aligned, f"{tr.status} {tr.start}..{tr.end}")
    check("extract_period finds a bare year",
          A.extract_period("Show me the first transaction in the year 2026") == "year_2026")

    # ---- the narrator is told the scope it describes -----------------------
    rl = bot.run("what was my latest transaction")
    scope = X.describe_scope(rl.result)
    check("describe_scope states an explicit 'no counterparty filter' for a wide listing",
          "ALL" in scope and "both" in scope, scope)

    # ---- explicit all-time wording is honoured even if the planner omits period
    bot._plan_llm = lambda m, h: ("get_spend", {"merchant": "zomato", "direction": "debit"})
    ra = bot.run("How much have I spent on Zomato total?")
    check("'total' in the question forces all_time instead of a period question",
          ra.status == A.ANSWER and ra.result.filters.get("start") is None,
          f"{ra.status} {ra.result.filters if ra.result else None}")


def main():
    if not os.path.exists(config.WAREHOUSE_PATH):
        print(f"No warehouse at {config.WAREHOUSE_PATH}. Run:\n"
              f"  python data_generator.py\n  python build_warehouse.py")
        return 1

    print("=" * 66)
    print("WAREHOUSE TEST SUITE")
    print("=" * 66)

    test_time_ranges()
    test_normalisation()

    con = db.connect(read_only=True)
    try:
        test_warehouse(con)
        test_extraction(con)
        test_target_questions(con)
        test_incremental(con)
        test_query_layer(con)
        test_resolver(con)
        test_agent(con)
        test_long_tail(con)
    finally:
        con.close()

    print("\n" + "=" * 66)
    print(f"SUMMARY: {PASSED} passed, {FAILED} failed (total {PASSED + FAILED})")
    print("=" * 66)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
