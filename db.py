"""
Database Layer (V2)
===================
DuckDB connection management, source loading (files or MySQL-over-TLS), the
dataset anchor date, and time-range resolution.

Time-range resolution is a deliberate rewrite of the V1 function that caused
BUGS.md B01. The V1 version returned (None, None) for BOTH "no period was
requested" and "a period was requested but I could not parse it", and the query
builder read that as "no filter" -- silently widening a one-month question to
all-time and answering with a 16x-too-large number at High Certainty.

Here those two cases are different statuses. An unparseable period returns
UNRESOLVED, which callers must surface as a clarifying question rather than
answering.
"""

import os
import re
import glob
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import duckdb
from dateutil.relativedelta import relativedelta

import config

# =============================================================
# CONNECTION
# =============================================================

def connect(path: str = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Opens the persistent warehouse, or an in-memory DB when path is ':memory:'."""
    target = path if path is not None else config.WAREHOUSE_PATH
    if target == ":memory:":
        return duckdb.connect(database=":memory:")
    return duckdb.connect(database=target, read_only=read_only)


def cursor(con: duckdb.DuckDBPyConnection):
    """
    Short-lived cursor for one query.

    BUGS.md B15: a single shared connection is not safe for concurrent use, and
    the agent issues several tool calls per turn. Every query goes through its
    own cursor.
    """
    return con.cursor()


def query_df(con: duckdb.DuckDBPyConnection, sql: str, params: list = None):
    """Executes parameterised SQL on a dedicated cursor and returns a DataFrame."""
    cur = con.cursor()
    try:
        return cur.execute(sql, params or []).df()
    finally:
        cur.close()


# =============================================================
# SOURCE LOADING
# =============================================================

def _find_source_file(data_dir: str, stem: str) -> Optional[str]:
    """Locates <stem>.parquet or <stem>.csv in data_dir."""
    for ext in ("parquet", "csv"):
        hits = glob.glob(os.path.join(data_dir, f"{stem}.{ext}"))
        if hits:
            return hits[0].replace("\\", "/")
    return None


def load_from_files(con, data_dir: str = None) -> dict:
    """
    Loads bank / account / transaction from parquet or CSV into raw_* tables.

    Returns {table_name: row_count}.
    """
    data_dir = data_dir or config.DATA_DIR
    counts = {}
    for table, meta in config.SCHEMA_CONFIG.items():
        path = _find_source_file(data_dir, meta["file"])
        if not path:
            raise FileNotFoundError(
                f"No source file for '{table}' in {data_dir} "
                f"(looked for {meta['file']}.parquet / .csv). "
                f"Run: python data_generator.py"
            )
        reader = "read_parquet" if path.endswith(".parquet") else "read_csv_auto"
        con.execute(f"CREATE OR REPLACE TABLE raw_{table} AS SELECT * FROM {reader}('{path}');")
        counts[table] = con.execute(f"SELECT COUNT(*) FROM raw_{table}").fetchone()[0]
    return counts


def load_from_mysql(con, dsn: dict = None, since: str = None) -> dict:
    """
    Streams source tables from MySQL over TLS via DuckDB's mysql extension.

    Nothing passes through Python memory. `since` (a date string) restricts the
    transaction pull for incremental loads -- callers should already have
    subtracted config.INGEST_LOOKBACK_DAYS from the watermark, because banking
    systems post transactions late (plan §12.3).
    """
    dsn = dsn or config.MYSQL_DSN
    if not dsn.get("host"):
        raise ValueError("MYSQL_HOST is not configured; use load_from_files() instead.")
    if not dsn.get("ssl_ca"):
        raise ValueError(
            "Refusing to connect without MYSQL_SSL_CA. Encryption in transit is "
            "required; set MYSQL_SSL_CA to the server CA bundle."
        )

    con.execute("INSTALL mysql; LOAD mysql;")
    attach = (
        f"host={dsn['host']} port={dsn['port']} user={dsn['user']} "
        f"password={dsn['password']} database={dsn['database']} "
        f"ssl_mode=VERIFY_IDENTITY ssl_ca={dsn['ssl_ca']}"
    )
    con.execute(f"ATTACH '{attach}' AS src (TYPE mysql, READ_ONLY);")

    date_col = config.SCHEMA_CONFIG["transaction"]["date_col"]
    counts = {}
    for table in ("bank", "account", "transaction"):
        # account.available_balance is a mutable snapshot, not an append-only
        # fact, so account and bank are always fully refreshed (plan §12.3).
        if table == "transaction" and since:
            sql = (f"CREATE OR REPLACE TABLE raw_transaction AS "
                   f"SELECT * FROM src.transaction WHERE {date_col} >= ?;")
            con.execute(sql, [since])
        else:
            con.execute(f"CREATE OR REPLACE TABLE raw_{table} AS SELECT * FROM src.{table};")
        counts[table] = con.execute(f"SELECT COUNT(*) FROM raw_{table}").fetchone()[0]

    con.execute("DETACH src;")
    return counts


# =============================================================
# ANCHOR DATE
# =============================================================

def get_anchor_date(con, table: str = None) -> date:
    """
    The dataset's notion of 'today' = MAX(transaction_date).

    Relative periods must anchor here, never to the wall clock: the sample data
    runs to 2026-06, and anchoring to datetime.now() would make 'last month'
    resolve to a window containing no rows.
    """
    table = table or config.TABLE_TXN_FACT
    date_col = config.SCHEMA_CONFIG["transaction"]["date_col"]
    try:
        val = con.execute(f"SELECT MAX({date_col}) FROM {table}").fetchone()[0]
    except Exception:
        try:
            val = con.execute(f"SELECT MAX({date_col}) FROM raw_transaction").fetchone()[0]
        except Exception:
            return date.today()
    if val is None:
        return date.today()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()


# =============================================================
# TIME RANGE RESOLUTION  (BUGS.md B01)
# =============================================================

RESOLVED = "RESOLVED"      # a concrete window
ALL_TIME = "ALL_TIME"      # no period requested -- querying all history is correct
UNRESOLVED = "UNRESOLVED"  # a period WAS requested but could not be parsed


@dataclass
class TimeRange:
    status: str
    start: Optional[str] = None
    end: Optional[str] = None
    label: str = ""
    canonical: Optional[str] = None
    suggestions: list = field(default_factory=list)

    @property
    def month_aligned(self) -> bool:
        """
        True when this window can be answered from the monthly rollup.

        Requires start on the 1st and end on a month end. An end at or beyond
        the anchor also counts: no data exists past the anchor, so extending to
        the month end is provably result-identical (plan §12.5).
        """
        if self.status != RESOLVED or not self.start or not self.end:
            return self.status == ALL_TIME
        s = datetime.strptime(self.start, "%Y-%m-%d").date()
        e = datetime.strptime(self.end, "%Y-%m-%d").date()
        last_of_end_month = (e.replace(day=1) + relativedelta(months=1)) - relativedelta(days=1)
        return s.day == 1 and e == last_of_end_month


def previous_window(tr: TimeRange) -> TimeRange:
    """
    The equal-length window immediately before `tr`.

    "Compare the last 3 months to the 3 months before that" needs Jan-Mar when
    the subject is Apr-Jun. Naming that window is awkward -- `previous_3_months`
    reads as a synonym of `last_3_months` and resolves to the same range -- so
    it is derived from the window being compared rather than parsed from words.
    That makes every phrasing of "the period before" work without enumerating
    them: "the 3 months before", "the previous period", "the same period last
    time".

    Month-aligned windows shift by whole calendar months so quarter-on-quarter
    comparisons stay on month boundaries (and on the fast rollup path). Other
    windows shift by their exact length in days.
    """
    if tr is None or tr.status != RESOLVED or not tr.start or not tr.end:
        return TimeRange(status=UNRESOLVED, label="the preceding period",
                         suggestions=_SUGGESTIONS)

    s = datetime.strptime(tr.start, "%Y-%m-%d").date()
    e = datetime.strptime(tr.end, "%Y-%m-%d").date()

    if tr.month_aligned:
        months = (e.year - s.year) * 12 + (e.month - s.month) + 1
        prev_start = s - relativedelta(months=months)
        prev_end = s - relativedelta(days=1)
        if months == 1:
            label = f"{prev_start:%B %Y}"
        else:
            label = (f"the {months} calendar months before that "
                     f"({prev_start:%b %Y} - {prev_end:%b %Y})")
        canonical = f"preceding_{months}_months"
    else:
        days = (e - s).days + 1
        prev_end = s - relativedelta(days=1)
        prev_start = prev_end - relativedelta(days=days - 1)
        label = f"the {days} days before that ({prev_start} to {prev_end})"
        canonical = f"preceding_{days}_days"

    return TimeRange(status=RESOLVED,
                     start=prev_start.strftime("%Y-%m-%d"),
                     end=prev_end.strftime("%Y-%m-%d"),
                     label=label, canonical=canonical)


def same_window(a: TimeRange, b: TimeRange) -> bool:
    """True when two ranges cover exactly the same dates."""
    if a is None or b is None:
        return False
    if a.status == ALL_TIME and b.status == ALL_TIME:
        return True
    return (a.status == b.status == RESOLVED
            and a.start == b.start and a.end == b.end)


# Canonical vocabulary. Aliases exist because the LLM is *prompted* with an enum
# but not *constrained* to it -- response_format=json_object guarantees syntax,
# not schema. Unknown values must land on UNRESOLVED, never on "no filter".
_PERIOD_ALIASES = {
    "all": "all_time", "all_time": "all_time", "total": "all_time",
    "lifetime": "all_time", "ever": "all_time", "overall": "all_time",
    "any": "all_time", "everything": "all_time", "none": "all_time",

    "this_month": "this_month", "current_month": "this_month",
    "mtd": "this_month", "month_to_date": "this_month",

    "last_month": "last_month", "previous_month": "last_month",
    "prev_month": "last_month", "past_month": "last_month",
    "prior_month": "last_month", "the_last_month": "last_month",

    "two_months_ago": "two_months_ago", "month_before_last": "two_months_ago",
    "month_before": "two_months_ago",

    "this_quarter": "this_quarter", "current_quarter": "this_quarter",
    "qtd": "this_quarter", "quarter_to_date": "this_quarter",

    "last_quarter": "last_quarter", "previous_quarter": "last_quarter",
    "past_quarter": "last_quarter", "prior_quarter": "last_quarter",

    "q1": "q1", "q2": "q2", "q3": "q3", "q4": "q4",

    "ytd": "ytd", "year_to_date": "ytd", "this_year": "ytd",
    "current_year": "ytd",

    "last_year": "last_year", "previous_year": "last_year",
    "past_year": "last_year", "prior_year": "last_year",
}

_MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_SUGGESTIONS = ["last_month", "last_3_months", "this_month", "ytd", "all_time"]


def _norm_period(period: str) -> str:
    """Folds 'Last Month', 'last-month', 'lastMonth', ' LAST_MONTH ' onto one form."""
    s = str(period).strip()
    # camelCase -> snake_case, so an LLM emitting 'lastMonth' still resolves.
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = s.lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _month_bounds(y: int, m: int):
    start = date(y, m, 1)
    end = (start + relativedelta(months=1)) - relativedelta(days=1)
    return start, end


def resolve_time_range(period, anchor_date: date) -> TimeRange:
    """
    Resolves a period expression against the dataset anchor date.

    Returns a TimeRange whose status is one of:
        ALL_TIME   - no period requested; querying all history is correct
        RESOLVED   - concrete start/end
        UNRESOLVED - a period was requested but not understood; the caller MUST
                     ask the user rather than answering (BUGS.md B01)
    """
    if period is None or (isinstance(period, str) and not period.strip()):
        return TimeRange(status=ALL_TIME, label="all time", canonical="all_time")

    p = _norm_period(period)
    canonical = _PERIOD_ALIASES.get(p)

    # last_n_months / past_3_months / trailing_6_months
    if canonical is None:
        m = re.match(r"^(?:last|past|previous|prior|trailing)_(\d{1,2})_months?$", p)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 60:
                start = (anchor_date.replace(day=1) - relativedelta(months=n - 1))
                _, end = _month_bounds(anchor_date.year, anchor_date.month)
                return TimeRange(
                    status=RESOLVED,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    label=f"last {n} calendar months ({start:%b %Y} - {anchor_date:%b %Y})",
                    canonical=f"last_{n}_months",
                )
            return TimeRange(status=UNRESOLVED, label=str(period), suggestions=_SUGGESTIONS)

    # last_n_days / last_30_days -- deliberately NOT month-aligned
    if canonical is None:
        m = re.match(r"^(?:last|past|previous|prior|trailing)_(\d{1,4})_days?$", p)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 3650:
                start = anchor_date - relativedelta(days=n - 1)
                return TimeRange(
                    status=RESOLVED,
                    start=start.strftime("%Y-%m-%d"),
                    end=anchor_date.strftime("%Y-%m-%d"),
                    label=f"last {n} days ({start} to {anchor_date})",
                    canonical=f"last_{n}_days",
                )
            return TimeRange(status=UNRESOLVED, label=str(period), suggestions=_SUGGESTIONS)

    # bare month name, optionally with a year: "april", "april_2026"
    if canonical is None:
        m = re.match(r"^([a-z]+)(?:_(\d{4}))?$", p)
        if m and m.group(1) in _MONTH_NAMES:
            mon = _MONTH_NAMES[m.group(1)]
            yr = int(m.group(2)) if m.group(2) else anchor_date.year
            start, end = _month_bounds(yr, mon)
            return TimeRange(
                status=RESOLVED,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                label=f"{start:%B %Y}",
                canonical=f"{m.group(1)}_{yr}",
            )

    if canonical is None:
        return TimeRange(status=UNRESOLVED, label=str(period), suggestions=_SUGGESTIONS)

    if canonical == "all_time":
        return TimeRange(status=ALL_TIME, label="all time", canonical="all_time")

    if canonical == "this_month":
        start, end = _month_bounds(anchor_date.year, anchor_date.month)
        return TimeRange(RESOLVED, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                         f"{start:%B %Y}", canonical)

    if canonical == "last_month":
        ref = anchor_date.replace(day=1) - relativedelta(months=1)
        start, end = _month_bounds(ref.year, ref.month)
        return TimeRange(RESOLVED, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                         f"{start:%B %Y}", canonical)

    if canonical == "two_months_ago":
        ref = anchor_date.replace(day=1) - relativedelta(months=2)
        start, end = _month_bounds(ref.year, ref.month)
        return TimeRange(RESOLVED, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                         f"{start:%B %Y}", canonical)

    if canonical in ("this_quarter", "last_quarter", "q1", "q2", "q3", "q4"):
        cur_q = (anchor_date.month - 1) // 3 + 1
        year = anchor_date.year
        if canonical == "this_quarter":
            q = cur_q
        elif canonical == "last_quarter":
            q = cur_q - 1
            if q < 1:
                q, year = 4, year - 1
        else:
            q = int(canonical[1])
        start = date(year, (q - 1) * 3 + 1, 1)
        end = (start + relativedelta(months=3)) - relativedelta(days=1)
        return TimeRange(RESOLVED, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                         f"Q{q} {year}", canonical)

    if canonical == "ytd":
        start = date(anchor_date.year, 1, 1)
        _, end = _month_bounds(anchor_date.year, anchor_date.month)
        return TimeRange(RESOLVED, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                         f"{anchor_date.year} year to date", canonical)

    if canonical == "last_year":
        y = anchor_date.year - 1
        return TimeRange(RESOLVED, f"{y}-01-01", f"{y}-12-31", str(y), canonical)

    return TimeRange(status=UNRESOLVED, label=str(period), suggestions=_SUGGESTIONS)


def parse_absolute_range(start: str, end: str, anchor_date: date = None) -> TimeRange:
    """
    Validates an explicit start/end pair (BUGS.md B11).

    V1 bound LLM-supplied dates straight into SQL: malformed values raised a raw
    DuckDB ConversionException that surfaced to the user, and inverted or
    wrong-year ranges silently returned zero rows.
    """
    try:
        s = datetime.strptime(str(start)[:10], "%Y-%m-%d").date()
        e = datetime.strptime(str(end)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return TimeRange(status=UNRESOLVED, label=f"{start} to {end}",
                         suggestions=["YYYY-MM-DD"])
    if s > e:
        s, e = e, s
    return TimeRange(RESOLVED, s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"),
                     f"{s} to {e}", "absolute")


# =============================================================
# MANIFEST
# =============================================================

def schema_fingerprint() -> str:
    """Detects schema drift between ingests."""
    blob = repr(sorted((t, tuple(sorted(m.items()))) for t, m in config.SCHEMA_CONFIG.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def read_manifest(con) -> Optional[dict]:
    try:
        rows = con.execute(f"SELECT * FROM {config.TABLE_MANIFEST} LIMIT 1").df()
    except Exception:
        return None
    return None if rows.empty else rows.iloc[0].to_dict()


def write_manifest(con, watermark, row_count: int):
    con.execute(f"""
        CREATE OR REPLACE TABLE {config.TABLE_MANIFEST} (
            watermark TIMESTAMP, row_count BIGINT,
            schema_hash VARCHAR, alias_map_version INTEGER, built_at TIMESTAMP
        );
    """)
    con.execute(
        f"INSERT INTO {config.TABLE_MANIFEST} VALUES (?, ?, ?, ?, now());",
        [watermark, row_count, schema_fingerprint(), config.ALIAS_MAP_VERSION],
    )
