"""
LIVE INGEST — import the organiser's data and rebuild the warehouse
===================================================================

    1. Paste the URL into DATABASE_URL at the top of datasource.py
    2. python ingest.py --check      # connect + validate, writes nothing
    3. python ingest.py --purge      # delete the dummy data, import theirs
    4. streamlit run app.py

Common variations:

    python ingest.py --url "mysql://user:pass@host:3306/db" --purge
    python ingest.py --purge --limit 100000     # quick smoke test first
    python ingest.py --purge --allow-insecure   # only if their endpoint has no TLS
    python ingest.py --incremental              # top up an existing warehouse

WHY --check FIRST: it verifies the connection and the schema without deleting
anything. Discovering a renamed column after wiping the demo data, live, is a
bad minute.

WHY --purge MATTERS: the shipped warehouse is generated dummy data. Without
--purge an import adds to it, and answers would mix real and fake transactions
-- worse than an obvious failure, because it looks like it worked.
"""

import os
import sys
import glob
import time
import shutil
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import db
import datasource
import build_warehouse


def _fmt(n):
    return f"{n:,}"


def _rule(title=""):
    print("\n" + "=" * 68)
    if title:
        print(title)
        print("=" * 68)


# ---------------------------------------------------------------- purge

def purge_targets():
    """Everything that would be removed, so it can be shown before deleting."""
    targets = []
    if os.path.exists(config.WAREHOUSE_PATH):
        targets.append((config.WAREHOUSE_PATH,
                        os.path.getsize(config.WAREHOUSE_PATH)))
    for pat in ("*.parquet", "*.csv"):
        for f in sorted(glob.glob(os.path.join(config.DATA_DIR, pat))):
            targets.append((f, os.path.getsize(f)))
    return targets


def purge(assume_yes: bool = False, keep_files: bool = False) -> bool:
    targets = purge_targets()
    if keep_files:
        targets = [t for t in targets if t[0] == config.WAREHOUSE_PATH]
    if not targets:
        print("Nothing to purge — no existing warehouse or data files.")
        return True

    total = sum(s for _, s in targets)
    print(f"\nThe following will be DELETED ({total / 1e6:.1f} MB):")
    for path, size in targets:
        print(f"   {os.path.relpath(path, config.BASE_DIR):<44} {size / 1e6:>8.1f} MB")

    if not assume_yes:
        try:
            reply = input("\nDelete these and continue? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Aborted. Nothing was deleted.")
            return False

    for path, _ in targets:
        try:
            os.remove(path)
        except OSError as e:
            print(f"   could not delete {path}: {e}")
    print(f"Deleted {len(targets)} file(s).")
    return True


def purge_chats(assume_yes: bool = False) -> bool:
    """
    Deletes saved conversations.

    Separate from --purge because chat history is not financial data: reloading
    the dataset should not silently destroy the conversations, but before a
    judged demo you may well want a clean slate.
    """
    path = config.CHAT_DB_PATH
    if not os.path.exists(path):
        print("No saved conversations to delete.")
        return True
    size = os.path.getsize(path)
    print(f"\nConversations: {os.path.relpath(path, config.BASE_DIR)} "
          f"({size / 1e6:.1f} MB)")
    if not assume_yes:
        try:
            reply = input("Delete all saved conversations? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("Kept.")
                return False
        except EOFError:
            return False
    try:
        os.remove(path)
        print("Conversations deleted.")
    except OSError as e:
        print(f"Could not delete: {e}")
        return False
    return True


# ---------------------------------------------------------------- connect

def connect_and_validate(url: str, allow_insecure: bool):
    """Attaches the source and checks its schema. Writes nothing."""
    dsn = datasource.parse_url(url)
    print(f"Source   : {datasource.describe(dsn)}")
    print(f"URL      : {datasource.redact(url)}")
    if allow_insecure:
        print("WARNING  : --allow-insecure — credentials and data may travel "
              "unencrypted.")

    scratch = db.connect(path=":memory:")
    alias = datasource.attach(scratch, dsn, allow_insecure=allow_insecure)

    if alias is None:  # a folder of files
        print("Tables   : reading .parquet/.csv from disk")
        return dsn, None, {"ok": True, "found": {}, "available": [],
                           "missing_tables": [], "missing_columns": {}}

    report = datasource.validate(scratch, alias)
    print(f"Tables   : {', '.join(report['available'][:12])}"
          f"{' …' if len(report['available']) > 12 else ''}")

    for logical, actual in report["found"].items():
        n = scratch.execute(f'SELECT COUNT(*) FROM {alias}."{actual}"').fetchone()[0]
        print(f"   {logical:<12} -> {actual:<24} {_fmt(n):>14} rows")

    if not report["ok"]:
        print("\nSCHEMA PROBLEMS")
        for t in report["missing_tables"]:
            print(f"   missing table : {t}")
            print(f"                   map it in datasource.TABLE_OVERRIDES")
        for t, cols in report["missing_columns"].items():
            print(f"   {t}: missing columns {', '.join(cols)}")
            print(f"                   rename them in config.SCHEMA_CONFIG['{t}']")
    else:
        print("Schema   : OK — all required tables and columns present")

    scratch.close()
    return dsn, alias, report


# ---------------------------------------------------------------- build

def run(url: str, allow_insecure: bool = False, limit: int = None,
        incremental: bool = False) -> dict:
    """
    Imports the source, then hands off to the normal build pipeline.

    The enrichment, alias-preservation and rollup logic lives in
    build_warehouse; this only supplies a different way of landing the raw
    tables. Re-implementing the build here would drift from it — in particular
    the incremental path's rule that a known merchant string keeps its existing
    canonical, without which a second ingest silently splits every merchant's
    history in two.
    """
    started = time.perf_counter()
    dsn = datasource.parse_url(url)

    def loader(con, since=None):
        alias = datasource.attach(con, dsn, allow_insecure=allow_insecure)
        counts = datasource.load_raw(con, alias, dsn, limit=limit, since=since)
        if alias:
            try:
                con.execute(f"DETACH {alias};")
            except Exception:
                pass
        if counts.get("transaction", 0) == 0 and not since:
            raise datasource.DataSourceError(
                "The source returned 0 transactions. Check the database name "
                "and that the account has read access.")
        return counts

    stats = (build_warehouse.incremental_build(loader=loader, verbose=True)
             if incremental else
             build_warehouse.full_build(loader=loader, verbose=True))

    con = db.connect(read_only=True)
    entities = con.execute(
        f"SELECT COUNT(DISTINCT entity_id) FROM {config.TABLE_TXN_FACT}").fetchone()[0]
    accounts = con.execute("SELECT COUNT(*) FROM raw_account").fetchone()[0]
    facts = con.execute(f"SELECT COUNT(*) FROM {config.TABLE_TXN_FACT}").fetchone()[0]
    anchor = db.get_anchor_date(con)
    con.close()

    _rule("READY")
    print(f"   transactions   {_fmt(facts)}")
    print(f"   customers      {_fmt(entities)}")
    print(f"   accounts       {_fmt(accounts)}")
    print(f"   anchor date    {anchor}  (relative dates resolve against this,")
    print(f"                  not today's date)")
    print(f"   coverage       {stats.get('coverage_rows', 0):.1%} of rows attributed "
          f"to a counterparty")
    print(f"   warehouse      {config.WAREHOUSE_PATH}")
    print(f"   elapsed        {time.perf_counter() - started:.1f}s")
    if limit:
        print(f"\n   NOTE: --limit {_fmt(limit)} was set. This is a partial import; "
              f"re-run without --limit for the full dataset.")
    print("\nNext:  streamlit run app.py")
    return {"facts": facts, "entities": entities, "anchor": anchor, **stats}


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(
        description="Import the organiser's database and rebuild the warehouse.")
    ap.add_argument("--url", help="Database URL (overrides datasource.py and env)")
    ap.add_argument("--check", action="store_true",
                    help="Connect and validate the schema. Writes nothing. "
                         "Run this first.")
    ap.add_argument("--purge", action="store_true",
                    help="Delete the existing warehouse and dummy data files "
                         "before importing.")
    ap.add_argument("--purge-only", action="store_true",
                    help="Delete the dummy data and stop.")
    ap.add_argument("--keep-files", action="store_true",
                    help="With --purge, delete the warehouse but keep data/ files.")
    ap.add_argument("--purge-chats", action="store_true",
                    help="Also delete saved conversations (chats.db). Chat history "
                         "is kept by default so a data reload does not wipe it.")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Skip the delete confirmation.")
    ap.add_argument("--limit", type=int,
                    help="Import at most N transactions (smoke test).")
    ap.add_argument("--incremental", action="store_true",
                    help="Top up an existing warehouse instead of rebuilding.")
    ap.add_argument("--allow-insecure", action="store_true",
                    help="Permit an unencrypted connection. Only if their "
                         "endpoint has no TLS.")
    args = ap.parse_args()

    try:
        if args.purge_only:
            _rule("PURGE")
            purge(assume_yes=args.yes, keep_files=args.keep_files)
            if args.purge_chats:
                purge_chats(assume_yes=args.yes)
            return 0

        url = datasource.resolve_url(args.url)

        _rule("CONNECT")
        _, _, report = connect_and_validate(url, args.allow_insecure)

        if args.check:
            print("\n--check: nothing was written. "
                  "Re-run with --purge to import.")
            return 0 if report["ok"] else 2

        if not report["ok"]:
            print("\nRefusing to import against a schema that does not match. "
                  "Fix the mappings above, or re-run --check.")
            return 2

        if args.purge:
            _rule("PURGE")
            if not purge(assume_yes=args.yes, keep_files=args.keep_files):
                return 1
            if args.purge_chats:
                purge_chats(assume_yes=args.yes)
        elif not args.incremental and os.path.exists(config.WAREHOUSE_PATH):
            print("\nNOTE: an existing warehouse will be replaced. Use --purge to "
                  "also remove the dummy data files in data/.")

        run(url, allow_insecure=args.allow_insecure, limit=args.limit,
            incremental=args.incremental)
        return 0

    except datasource.DataSourceError as e:
        print(f"\nERROR\n{e}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
