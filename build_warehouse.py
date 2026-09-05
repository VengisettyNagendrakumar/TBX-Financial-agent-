"""
Warehouse Builder
=================
Orchestrates the ingest described in ARCHITECTURE_V2.md §12:

    land -> extract -> normalise vocabulary -> map -> sort -> roll up -> persist

Full build (first run):
    python build_warehouse.py
    python build_warehouse.py --source mysql

Incremental (nth run):
    python build_warehouse.py --incremental

Incremental loads re-scan config.INGEST_LOOKBACK_DAYS behind the stored
watermark and dedupe on the primary key, because banking systems post
transactions late: a transaction dated the 3rd can arrive on the 9th, and a
strict `> watermark` filter would lose it permanently.

The monthly rollup is always rebuilt wholesale. Measured at 4M rows a full
rebuild (~0.16s) is *faster* than appending a 50k delta, so incremental rollup
logic would be more code, slower, and able to drift out of sync with the facts.
"""

import os
import sys
import time
import argparse
from datetime import timedelta

import config
import db
import enrichment


def _fmt(n):
    return f"{n:,}"


def full_build(source: str = "files", data_dir: str = None, verbose: bool = True,
               loader=None) -> dict:
    """
    Builds the warehouse from scratch, replacing any existing one.

    `loader(con, since=None)` overrides the built-in file/MySQL loaders so a
    live URL (see ingest.py) reuses this pipeline rather than duplicating it.
    """
    started = time.perf_counter()
    if os.path.exists(config.WAREHOUSE_PATH):
        os.remove(config.WAREHOUSE_PATH)
    con = db.connect()

    if verbose:
        print("=" * 66)
        print("FULL BUILD")
        print("=" * 66)

    t0 = time.perf_counter()
    counts = (loader(con) if loader is not None
              else db.load_from_mysql(con) if source == "mysql"
              else db.load_from_files(con, data_dir))
    load_s = time.perf_counter() - t0
    if verbose:
        print(f"\n[1] Land ({source})  {load_s:.2f}s")
        for t, n in counts.items():
            print(f"      raw_{t:12} {_fmt(n):>12} rows")

    if verbose:
        print("\n[2] Enrich")
    stats = enrichment.enrich(con, verbose=verbose)

    t0 = time.perf_counter()
    enrichment.build_rollup(con)
    rollup_s = time.perf_counter() - t0
    rollup_rows = con.execute(
        f"SELECT COUNT(*) FROM {config.TABLE_ROLLUP_MONTHLY}").fetchone()[0]
    dim_rows = con.execute(
        f"SELECT COUNT(*) FROM {config.TABLE_MERCHANT_DIM}").fetchone()[0]
    if verbose:
        print(f"\n[3] Aggregate     {rollup_s:.2f}s")
        print(f"      {config.TABLE_ROLLUP_MONTHLY:16} {_fmt(rollup_rows):>12} rows "
              f"(from {_fmt(stats['rows'])} facts)")
        print(f"      {config.TABLE_MERCHANT_DIM:16} {_fmt(dim_rows):>12} rows")

    anchor = db.get_anchor_date(con)
    watermark = con.execute(
        f"SELECT MAX({config.SCHEMA_CONFIG['transaction']['date_col']}) "
        f"FROM {config.TABLE_TXN_FACT}").fetchone()[0]
    db.write_manifest(con, watermark, stats["rows"])

    total_s = time.perf_counter() - started
    if verbose:
        print(f"\n[4] Persist       {config.WAREHOUSE_PATH}")
        print(f"      anchor date     {anchor}")
        print(f"      alias version   {config.ALIAS_MAP_VERSION}")
        print(f"\nTotal {total_s:.2f}s")
        _print_coverage_detail(con)
    con.close()

    stats.update({"total_s": total_s, "rollup_rows": rollup_rows, "anchor": anchor})
    return stats


def incremental_build(source: str = "files", data_dir: str = None,
                      verbose: bool = True, loader=None) -> dict:
    """
    Appends new transactions, re-maps merchants, rebuilds the rollup.

    Three correctness rules, all of which are easy to get wrong:
      1. Re-scan a lookback window and dedupe on the PK (late posting).
      2. Map new merchant strings against the EXISTING canonical set first --
         otherwise 'SWIGGY' in the delta forms a second canonical and silently
         splits every historical total.
      3. account/bank are fully refreshed; available_balance is a mutable
         snapshot, not an append-only fact, so a delta is meaningless for it.
    """
    started = time.perf_counter()
    if not os.path.exists(config.WAREHOUSE_PATH):
        if verbose:
            print("No warehouse found; falling back to a full build.")
        return full_build(source=source, data_dir=data_dir, verbose=verbose,
                          loader=loader)

    con = db.connect()
    manifest = db.read_manifest(con)
    txn = config.SCHEMA_CONFIG["transaction"]

    if verbose:
        print("=" * 66)
        print("INCREMENTAL BUILD")
        print("=" * 66)

    # -- schema / alias drift -------------------------------------------
    if manifest is not None:
        if manifest.get("schema_hash") != db.schema_fingerprint():
            if verbose:
                print("Schema fingerprint changed -> full rebuild required.")
            con.close()
            return full_build(source=source, data_dir=data_dir, verbose=verbose,
                              loader=loader)
        if int(manifest.get("alias_map_version", 0)) != config.ALIAS_MAP_VERSION:
            if verbose:
                print(f"Alias map v{manifest.get('alias_map_version')} -> "
                      f"v{config.ALIAS_MAP_VERSION}: historical answers change, "
                      f"so re-mapping every row -> full rebuild.")
            con.close()
            return full_build(source=source, data_dir=data_dir, verbose=verbose,
                              loader=loader)

    watermark = manifest["watermark"] if manifest is not None else None
    since = None
    if watermark is not None:
        since = (watermark - timedelta(days=config.INGEST_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if verbose:
        print(f"\n[1] Watermark {watermark} "
              f"(re-scanning {config.INGEST_LOOKBACK_DAYS}d back from {since})")

    t0 = time.perf_counter()
    if loader is not None:
        loader(con, since=since)
    elif source == "mysql":
        db.load_from_mysql(con, since=since)
    else:
        db.load_from_files(con, data_dir)
        if since:
            con.execute(f"DELETE FROM raw_transaction WHERE {txn['date_col']} < '{since}';")
    landed = con.execute("SELECT COUNT(*) FROM raw_transaction").fetchone()[0]
    if verbose:
        print(f"      landed {_fmt(landed)} candidate rows in {time.perf_counter()-t0:.2f}s")

    before = con.execute(f"SELECT COUNT(*) FROM {config.TABLE_TXN_FACT}").fetchone()[0]

    # -- enrich the delta into a staging fact table ----------------------
    t0 = time.perf_counter()
    con.execute(f"ALTER TABLE {config.TABLE_TXN_FACT} RENAME TO _fact_existing;")
    existing_alias = con.execute(
        f"SELECT * FROM {config.TABLE_MERCHANT_ALIAS}").fetchall()
    enrichment.enrich(con, verbose=False)
    con.execute(f"ALTER TABLE {config.TABLE_TXN_FACT} RENAME TO _fact_delta;")

    # Rule 2: preserve the existing raw->canonical decisions. Re-inserting them
    # after the delta's rows means an already-known raw string keeps its old
    # canonical, so no merchant ever splits in two across ingests.
    if existing_alias:
        con.execute(f"""
            DELETE FROM {config.TABLE_MERCHANT_ALIAS}
            WHERE merchant_raw IN (SELECT merchant_raw FROM (
                SELECT UNNEST(?::VARCHAR[]) AS merchant_raw));
        """, [[r[0] for r in existing_alias]])
        con.executemany(
            f"INSERT INTO {config.TABLE_MERCHANT_ALIAS} VALUES (?, ?, ?, ?);",
            existing_alias)

    # Rule 1: anti-join on the primary key.
    con.execute(f"ALTER TABLE _fact_existing RENAME TO {config.TABLE_TXN_FACT};")
    con.execute(f"""
        INSERT INTO {config.TABLE_TXN_FACT}
        SELECT d.* FROM _fact_delta d
        WHERE NOT EXISTS (
            SELECT 1 FROM {config.TABLE_TXN_FACT} f
            WHERE f.{txn['id_col']} = d.{txn['id_col']});
    """)
    con.execute("DROP TABLE IF EXISTS _fact_delta;")
    after = con.execute(f"SELECT COUNT(*) FROM {config.TABLE_TXN_FACT}").fetchone()[0]
    append_s = time.perf_counter() - t0
    if verbose:
        print(f"\n[2] Append        {append_s:.2f}s")
        print(f"      {_fmt(landed)} candidates -> {_fmt(after - before)} new "
              f"({_fmt(landed - (after - before))} already present)")

    # Rule 3 + rollup rebuild.
    t0 = time.perf_counter()
    enrichment.build_merchant_dim(con)
    enrichment.build_rollup(con)
    rebuild_s = time.perf_counter() - t0
    if verbose:
        print(f"\n[3] Rebuild dim + rollup  {rebuild_s:.2f}s (wholesale, cannot drift)")

    new_watermark = con.execute(
        f"SELECT MAX({txn['date_col']}) FROM {config.TABLE_TXN_FACT}").fetchone()[0]
    db.write_manifest(con, new_watermark, after)
    total_s = time.perf_counter() - started
    if verbose:
        print(f"\n[4] Watermark -> {new_watermark}")
        print(f"\nTotal {total_s:.2f}s")
    con.close()
    return {"before": before, "after": after, "added": after - before, "total_s": total_s}


def _print_coverage_detail(con):
    print("\n" + "-" * 66)
    print("COVERAGE BY COUNTERPARTY KIND")
    print("-" * 66)
    print(con.execute(f"""
        SELECT counterparty_kind,
               COUNT(*)                      AS txns,
               ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct,
               COUNT(DISTINCT merchant_norm) AS distinct_names
        FROM {config.TABLE_TXN_FACT}
        GROUP BY 1 ORDER BY txns DESC;
    """).df().to_string(index=False))
    print("\nTOP MERCHANTS BY DEBIT SPEND")
    print(con.execute(f"""
        SELECT merchant_norm, counterparty_kind,
               ROUND(SUM(total_amount), 2) AS spend, SUM(txn_count) AS txns
        FROM {config.TABLE_ROLLUP_MONTHLY}
        WHERE transaction_type = '{config.TXN_DEBIT}'
          AND counterparty_kind NOT IN {config.KINDS_EXCLUDED_FROM_SPEND_RANKING}
        GROUP BY 1, 2 ORDER BY spend DESC LIMIT 8;
    """).df().to_string(index=False))
    print("\nPEOPLE WHO SENT MONEY IN (drives 'how much did my friend pay me')")
    print(con.execute(f"""
        SELECT merchant_norm AS person, ROUND(SUM(total_amount), 2) AS received,
               SUM(txn_count) AS txns
        FROM {config.TABLE_ROLLUP_MONTHLY}
        WHERE transaction_type = '{config.TXN_CREDIT}'
          AND counterparty_kind = '{config.KIND_PERSON}'
        GROUP BY 1 ORDER BY received DESC LIMIT 6;
    """).df().to_string(index=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the analytical warehouse.")
    ap.add_argument("--source", choices=["files", "mysql"], default="files")
    ap.add_argument("--incremental", action="store_true")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    try:
        if args.incremental:
            incremental_build(source=args.source, data_dir=args.data_dir)
        else:
            full_build(source=args.source, data_dir=args.data_dir)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
