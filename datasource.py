"""
DATA SOURCE — edit this file, then run:  python ingest.py --purge
=================================================================

THE ONE PLACE TO PASTE THE ORGANISER'S DATABASE URL.

    DATABASE_URL = "mysql://user:password@host:3306/dbname"

Everything else has a working default. You can also pass it on the command line
(`python ingest.py --url "..."`) or set TBX_DATABASE_URL, either of which wins
over the value here.

Supported URL shapes
--------------------
    mysql://user:pass@host:3306/dbname          MySQL / MariaDB
    postgresql://user:pass@host:5432/dbname     PostgreSQL
    sqlite:///absolute/path/to/file.db          SQLite file
    duckdb:///absolute/path/to/file.duckdb      DuckDB file
    file:///absolute/path/to/folder             folder of .parquet / .csv

Query parameters are passed through, so `?ssl_mode=REQUIRED` works.

TLS
---
Connections are encrypted by default (REQUIRE_TLS below). If the organiser's
endpoint has no TLS, the connection will fail with a clear message and you can
re-run with `--allow-insecure`. That flag exists because losing the round to a
refused connection is worse than an unencrypted hop to a test database on a
hackathon LAN -- but it prints a warning every time, and it is never the
default.
"""

import os
import re
from urllib.parse import urlparse, parse_qsl, unquote

import config

# ---------------------------------------------------------------------------
# PASTE THE URL HERE
# ---------------------------------------------------------------------------
DATABASE_URL = ""

# Path to the server's CA bundle, if they give you one. Optional.
SSL_CA = ""

# Refuse to send credentials over an unencrypted link unless overridden.
REQUIRE_TLS = True

# If their tables are named differently, map them here:
#   {"transaction": "txns", "account": "accounts"}
# Column names live in config.SCHEMA_CONFIG.
TABLE_OVERRIDES = {}

REQUIRED_TABLES = ("bank", "account", "transaction")


class DataSourceError(RuntimeError):
    """Connection or schema problem, phrased for someone under time pressure."""


def resolve_url(cli_url: str = None) -> str:
    """CLI flag > environment > this file."""
    url = (cli_url or os.getenv("TBX_DATABASE_URL", "") or DATABASE_URL or "").strip()
    if not url:
        raise DataSourceError(
            "No database URL. Do one of:\n"
            "  1. Paste it into DATABASE_URL at the top of datasource.py\n"
            "  2. python ingest.py --url \"mysql://user:pass@host:3306/db\"\n"
            "  3. set TBX_DATABASE_URL=..."
        )
    return url


def parse_url(url: str) -> dict:
    """Splits a URL into the parts DuckDB's ATTACH needs."""
    u = urlparse(url)
    scheme = (u.scheme or "").lower()
    # Tolerate SQLAlchemy-style drivers: mysql+pymysql -> mysql
    dialect = scheme.split("+")[0]

    aliases = {"mysql": "mysql", "mariadb": "mysql",
               "postgresql": "postgres", "postgres": "postgres", "psql": "postgres",
               "sqlite": "sqlite", "sqlite3": "sqlite",
               "duckdb": "duckdb", "file": "files", "": "files"}
    if dialect not in aliases:
        raise DataSourceError(
            f"Unsupported URL scheme {scheme!r}. Supported: mysql, postgresql, "
            f"sqlite, duckdb, file.")
    kind = aliases[dialect]

    params = dict(parse_qsl(u.query or ""))

    if kind in ("sqlite", "duckdb", "files"):
        # sqlite:///C:/path -> /C:/path ; strip the leading slash on Windows
        path = unquote(u.path or "")
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        if u.netloc and not path:
            path = u.netloc
        return {"kind": kind, "path": path, "params": params, "raw": url}

    if not u.hostname:
        raise DataSourceError(f"No host in URL: {url!r}")

    return {
        "kind": kind,
        "host": u.hostname,
        "port": u.port or (3306 if kind == "mysql" else 5432),
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": (u.path or "").lstrip("/"),
        "params": params,
        "raw": url,
    }


def redact(url: str) -> str:
    """A URL safe to print or paste into a screenshot."""
    return re.sub(r"://([^:/@]+):([^@]*)@", r"://\1:****@", url or "")


def describe(dsn: dict) -> str:
    if dsn["kind"] in ("sqlite", "duckdb", "files"):
        return f"{dsn['kind']} at {dsn['path']}"
    return (f"{dsn['kind']} {dsn['user']}@{dsn['host']}:{dsn['port']}"
            f"/{dsn['database']}")


def _tls_clause(dsn: dict, allow_insecure: bool) -> str:
    """TLS settings for the ATTACH string, honouring any ?ssl_mode= in the URL."""
    if dsn["kind"] not in ("mysql", "postgres"):
        return ""
    params = dsn.get("params", {})
    ca = SSL_CA or os.getenv("TBX_SSL_CA", "") or params.get("ssl_ca", "")

    if allow_insecure or not REQUIRE_TLS:
        if dsn["kind"] == "mysql":
            return f" ssl_mode={params.get('ssl_mode', 'PREFERRED')}"
        return f" sslmode={params.get('sslmode', 'prefer')}"

    if dsn["kind"] == "mysql":
        mode = params.get("ssl_mode", "VERIFY_IDENTITY" if ca else "REQUIRED")
        return f" ssl_mode={mode}" + (f" ssl_ca={ca}" if ca else "")
    mode = params.get("sslmode", "verify-full" if ca else "require")
    return f" sslmode={mode}" + (f" sslrootcert={ca}" if ca else "")


def attach(con, dsn: dict, allow_insecure: bool = False, alias: str = "src"):
    """
    Attaches the source read-only as `alias`.

    Uses DuckDB's own mysql/postgres/sqlite extensions so rows stream straight
    into the warehouse without passing through Python memory -- which is what
    keeps a multi-million-row import to seconds rather than minutes.
    """
    kind = dsn["kind"]
    if kind == "files":
        return None  # handled by the file loader

    if kind == "duckdb":
        con.execute(f"ATTACH '{dsn['path']}' AS {alias} (READ_ONLY);")
        return alias

    ext = {"mysql": "mysql", "postgres": "postgres", "sqlite": "sqlite"}[kind]
    try:
        con.execute(f"INSTALL {ext};")
    except Exception:
        pass  # already installed, or offline with it bundled
    con.execute(f"LOAD {ext};")

    if kind == "sqlite":
        con.execute(f"ATTACH '{dsn['path']}' AS {alias} (TYPE sqlite, READ_ONLY);")
        return alias

    conn_str = (f"host={dsn['host']} port={dsn['port']} user={dsn['user']} "
                f"password={dsn['password']} "
                f"{'database' if kind == 'mysql' else 'dbname'}={dsn['database']}"
                f"{_tls_clause(dsn, allow_insecure)}")
    try:
        con.execute(f"ATTACH '{conn_str}' AS {alias} (TYPE {ext}, READ_ONLY);")
    except Exception as e:
        msg = str(e)
        if not allow_insecure and re.search(r"ssl|tls|secure", msg, re.I):
            raise DataSourceError(
                f"Could not establish an encrypted connection:\n  {msg}\n\n"
                f"If the organiser's endpoint has no TLS, re-run with "
                f"--allow-insecure.") from e
        raise DataSourceError(f"Could not connect to {describe(dsn)}:\n  {msg}") from e
    return alias


def source_table(logical: str) -> str:
    return TABLE_OVERRIDES.get(logical, logical)


def list_tables(con, alias: str) -> list:
    try:
        return [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = ?",
            [alias]).fetchall()]
    except Exception:
        return [r[0] for r in con.execute(f"SHOW TABLES FROM {alias};").fetchall()]


def validate(con, alias: str) -> dict:
    """
    Checks the source has the tables and columns we read, BEFORE deleting
    anything. Returns a report rather than raising, so one run can show every
    problem at once instead of one per attempt.
    """
    available = list_tables(con, alias)
    lowered = {t.lower(): t for t in available}
    report = {"available": available, "missing_tables": [],
              "missing_columns": {}, "found": {}, "ok": True}

    for logical in REQUIRED_TABLES:
        want = source_table(logical)
        actual = lowered.get(want.lower())
        if actual is None:
            report["missing_tables"].append(want)
            report["ok"] = False
            continue
        report["found"][logical] = actual
        cols = {r[0].lower() for r in
                con.execute(f'DESCRIBE {alias}."{actual}";').fetchall()}
        needed = [v for k, v in config.SCHEMA_CONFIG[logical].items()
                  if k.endswith("_col")]
        missing = [c for c in needed if c.lower() not in cols]
        if missing:
            report["missing_columns"][logical] = missing
            report["ok"] = False
    return report


def load_raw(con, alias: str, dsn: dict, limit: int = None, since: str = None) -> dict:
    """
    Copies the source tables into raw_* in the warehouse.

    `account` and `bank` are always fully refreshed: available_balance is a
    mutable snapshot, not an append-only fact, so a delta of it is meaningless.
    """
    if dsn["kind"] == "files":
        import db
        return db.load_from_files(con, dsn["path"] or config.DATA_DIR)

    date_col = config.SCHEMA_CONFIG["transaction"]["date_col"]
    counts = {}
    for logical in REQUIRED_TABLES:
        src = f'{alias}."{source_table(logical)}"'
        where, params = "", []
        if logical == "transaction" and since:
            where, params = f" WHERE {date_col} >= ?", [since]
        cap = f" LIMIT {int(limit)}" if (limit and logical == "transaction") else ""
        con.execute(f"CREATE OR REPLACE TABLE raw_{logical} AS "
                    f"SELECT * FROM {src}{where}{cap};", params)
        counts[logical] = con.execute(
            f"SELECT COUNT(*) FROM raw_{logical}").fetchone()[0]
    return counts
