"""
Synthetic Dataset Generator (V2 — bank / account / transaction)
================================================================
Generates data matching the hackathon schema, with narration strings that
reproduce the real formats seen in the provided sample:

    UPI-NAVYUG SELECTION-XXXXXX8672-AUBL0002125-103293775381-260514201735136
    NEFT  - UTIB0002678 - 95604250 - 915020031685136 - UMANG SELECTIONHAPUR
    IMPS/P2A/600228462725/UTIB/918020101986700/00/INET/9211/SELECTIONMALIGAI/...
    IMPS OW/507614422198/Gautam singh/SBIN/43292707719
    FT -  95842568 -  50200013729069 - SELECTION ELECTRONICS   DAHISAR EAST
    R/RATNR52025121600100235/ZBFLCTP405PBL15667333//SELECTRICITY TWO PRIVATE...
    NEFT/000483399203/ICIC/PARESH VIKRANT GHASE

Generation runs *inside DuckDB* rather than a Python loop: 4M rows take a couple
of seconds vectorised, where a row-by-row loop takes minutes.

The mix is deliberately imperfect. It includes brand/legal-name variants
(BUNDL TECHNOLOGIES vs SWIGGY), legal suffixes, trailing city names, bank
charges, self-transfers and a slice of genuinely unparseable narration, so that
merchant-extraction coverage is a real measurement rather than 100% by
construction.

Usage:
    python data_generator.py                        # 200k transactions
    python data_generator.py --rows 4000000         # full scale
    python data_generator.py --rows 5000 --format csv
"""

import os
import argparse
import time

import duckdb

import config

# Merchants a user would ask about by brand name.
MERCHANTS = [
    "SWIGGY", "ZOMATO", "AMAZON WEB SERVICES", "AMAZON LOGISTICS", "FLIPKART", "BLINKIT", "MYNTRA", "BIGBASKET",
    "DMART", "RELIANCE DIGITAL", "CROMA", "IRCTC", "MAKEMYTRIP", "UBER",
    "OLA", "RAPIDO", "BOOKMYSHOW", "NETFLIX", "SPOTIFY", "JIO", "AIRTEL",
    "TATA POWER", "APOLLO PHARMACY", "PHARMEASY", "NYKAA", "DECATHLON",
    "STARBUCKS", "DOMINOS", "MCDONALDS", "HALDIRAM", "SELECTION ELECTRONICS",
    "NAVYUG SELECTION", "SELECTRICITY TWO", "UMANG SELECTION", "LENSKART",
    "URBAN COMPANY", "ZEPTO", "LICIOUS", "CULT FIT", "INDIGO", "VISTARA",
]

# Legal-entity names that must fold onto a brand via config.MERCHANT_ALIASES.
# If normalisation misses these, Swiggy spend silently splits in two.
LEGAL_VARIANTS = [
    "BUNDL TECHNOLOGIES", "SWIGGY INSTAMART", "ETERNAL", "ZOMATO MEDIA",
    "BLINK COMMERCE", "AMAZON SELLER SERVICES", "FLIPKART INTERNET",
    "UBER INDIA SYSTEMS", "ANI TECHNOLOGIES",
]

# Individuals — these drive "how much did my friend pay me".
PERSONS = [
    "Gautam Singh", "Paresh Vikrant Ghase", "Ananya Sharma", "Rohit Mehta",
    "Priya Nair", "Vikram Iyer", "Sneha Kulkarni", "Arjun Patel",
    "Meera Krishnan", "Sanjay Gupta", "Divya Reddy", "Karan Malhotra",
]

BANK_CHARGES = [
    "IMPS charges", "NEFT CHARGES", "AMC FEE", "SMS ALERT CHARGES",
    "GST ON CHARGES", "ATM FEE", "MIN BAL PENALTY", "ANNUAL FEE",
]

CITIES = [
    "DAHISAR EAST", "SAKET DELHI", "KORAMANGALA", "ANDHERI WEST", "HAPUR",
    "BANJARA HILLS", "SALT LAKE", "VIMAN NAGAR", "T NAGAR", "MALIGAI",
]

SUFFIXES = ["", "", "", " PRIVATE LIMITED", " LTD", " PVT LTD", " LIMITED"]

BANKS = [
    ("HDFC", "HDFC BANK LIMITED"), ("ICIC", "ICICI BANK LIMITED"),
    ("SBIN", "STATE BANK OF INDIA"), ("UTIB", "AXIS BANK LIMITED"),
    ("KKBK", "KOTAK MAHINDRA BANK LIMITED"), ("CNRB", "CANARA BANK"),
    ("UBIN", "UNION BANK OF INDIA"), ("AUBL", "AU SMALL FINANCE BANK LIMITED"),
    ("TMBL", "TAMILNAD MERCANTILE BANK LIMITED"), ("RATN", "RBL BANK LIMITED"),
]


def _sql_array(values):
    escaped = [str(v).replace("'", "''") for v in values]
    return "[" + ", ".join(f"'{v}'" for v in escaped) + "]"


def generate(rows: int = 200_000, entities: int = 50, accounts: int = 200,
             months: int = 24, out_dir: str = None, fmt: str = "parquet",
             anchor: str = "2026-06-24", seed: int = 42) -> dict:
    out_dir = out_dir or config.DATA_DIR
    os.makedirs(out_dir, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"SELECT setseed({(seed % 1000) / 1000.0});")

    t0 = time.perf_counter()

    # ---------- bank ----------
    con.execute("CREATE TABLE bank (bank_code VARCHAR, bank_name VARCHAR);")
    con.executemany("INSERT INTO bank VALUES (?, ?);", BANKS)

    # ---------- account ----------
    con.execute(f"""
        CREATE TABLE account AS
        SELECT
            format('acc{{:08d}}-0000-4000-8000-{{:012d}}', i, i)      AS account_id,
            format('ent{{:08d}}-0000-4000-8000-{{:012d}}',
                   i % {entities}, i % {entities})                     AS entity_id,
            (50200000000000 + i * 7919)::VARCHAR                       AS account_number,
            ([21, 4, 46])[(i % 3) + 1]                                 AS program_id,
            round((random() * 4000000 - 500000)::DECIMAL(15,2), 2)     AS available_balance,
            ({_sql_array([b[0] for b in BANKS])})[(i % {len(BANKS)}) + 1] AS bank_code
        FROM range(0, {accounts}) t(i);
    """)

    # ---------- transaction ----------
    # Counterparty pools, chosen per row by a bucket on i.
    con.execute(f"""
        CREATE TABLE transaction AS
        WITH base AS (
            SELECT
                i,
                -- Independently salted hashes. Deriving everything from one
                -- linear expression correlates account choice with merchant
                -- choice, which leaves each entity transacting with only a
                -- handful of merchants -- unrealistic, and it makes the
                -- resolver look broken when it is not.
                (hash(i::VARCHAR || 'acct')   % 1000000)::BIGINT AS h,
                (hash(i::VARCHAR || 'when')   % 1000000)::BIGINT AS h_date,
                (hash(i::VARCHAR || 'amount') % 1000000)::BIGINT AS h_amt,
                (hash(i::VARCHAR || 'bucket') % 100)::BIGINT     AS bucket,
                ({_sql_array(MERCHANTS)})[((hash(i::VARCHAR || 'm') % {len(MERCHANTS)})::BIGINT + 1)]        AS merchant,
                ({_sql_array(LEGAL_VARIANTS)})[((hash(i::VARCHAR || 'lv') % {len(LEGAL_VARIANTS)})::BIGINT + 1)] AS legal_variant,
                ({_sql_array(PERSONS)})[((hash(i::VARCHAR || 'p') % {len(PERSONS)})::BIGINT + 1)]           AS person,
                ({_sql_array(BANK_CHARGES)})[((hash(i::VARCHAR || 'bc') % {len(BANK_CHARGES)})::BIGINT + 1)]  AS charge,
                ({_sql_array(CITIES)})[((hash(i::VARCHAR || 'c') % {len(CITIES)})::BIGINT + 1)]              AS city,
                ({_sql_array(SUFFIXES)})[((hash(i::VARCHAR || 's') % {len(SUFFIXES)})::BIGINT + 1)]         AS suffix,
                ({_sql_array([b[0] for b in BANKS])})[((hash(i::VARCHAR || 'b') % {len(BANKS)})::BIGINT + 1)] AS ifsc_bank
            FROM range(0, {rows}) t(i)
        ),
        typed AS (
            -- counterparty bucket: 0-64 merchant, 65-74 legal variant,
            -- 75-84 person, 85-90 bank charge, 91-93 self transfer,
            -- 94-99 unparseable junk
            SELECT * FROM base
        )
        SELECT
            format('{{:08x}}-{{:04x}}-4{{:03x}}-8{{:03x}}-{{:012x}}',
                   i, i % 65536, i % 4096, (i * 7) % 4096, i)          AS transaction_id,
            format('acc{{:08d}}-0000-4000-8000-{{:012d}}',
                   h % {accounts}, h % {accounts})                     AS account_id,
            (TIMESTAMP '{anchor} 23:59:59'
                - INTERVAL (h_date % {months * 30}) DAY
                - INTERVAL (h_date % 86400) SECOND)                         AS transaction_date,
            CASE WHEN bucket BETWEEN 75 AND 84 THEN 'credit'
                 WHEN h % 23 = 0 THEN 'credit'
                 ELSE 'debit' END                                      AS transaction_type,
            CASE
              WHEN bucket <= 64 THEN
                CASE h % 6
                  WHEN 0 THEN 'UPI-' || merchant || suffix || '-XXXXXX'
                           || (h % 10000)::VARCHAR || '-' || ifsc_bank
                           || '000' || (h % 9999)::VARCHAR || '-'
                           || (500000000000 + h)::VARCHAR || '-2605142017'
                  WHEN 1 THEN 'NEFT  - ' || ifsc_bank || '000' || (h % 9999)::VARCHAR
                           || ' - ' || (95000000 + h)::VARCHAR
                           || ' - ' || (915020031685136 + h)::VARCHAR
                           || ' - ' || merchant || suffix
                  WHEN 2 THEN 'IMPS/P2A/' || (600000000000 + h)::VARCHAR || '/'
                           || ifsc_bank || '/' || (918020101986700 + h)::VARCHAR
                           || '/00/INET/' || (h % 9999)::VARCHAR || '/'
                           || merchant || '/ZBFLCTP5L2PBL' || (h % 99999)::VARCHAR || '/INWD48'
                  WHEN 3 THEN 'FT -  ' || (95000000 + h)::VARCHAR || ' -  '
                           || (50200013729069 + h)::VARCHAR || ' - '
                           || merchant || '   ' || city
                  WHEN 4 THEN 'R/' || ifsc_bank || 'R5' || (h % 999999)::VARCHAR
                           || '/ZBFLCTP405PBL' || (h % 99999)::VARCHAR || '//'
                           || merchant || suffix || '/REF' || (h % 9999)::VARCHAR
                  ELSE 'NEFT/' || (h % 999999999)::VARCHAR || '/' || ifsc_bank
                           || '/' || merchant || suffix
                END
              WHEN bucket <= 74 THEN
                CASE h % 3
                  WHEN 0 THEN 'UPI-' || legal_variant || '-XXXXXX' || (h % 10000)::VARCHAR
                           || '-' || ifsc_bank || '0002125-' || (500000000000 + h)::VARCHAR
                  WHEN 1 THEN 'NEFT  - ' || ifsc_bank || '0002678 - ' || (95000000 + h)::VARCHAR
                           || ' - ' || (915020031685136 + h)::VARCHAR || ' - ' || legal_variant
                  ELSE 'FT -  ' || (95000000 + h)::VARCHAR || ' -  '
                           || (50200013729069 + h)::VARCHAR || ' - ' || legal_variant
                           || ' PRIVATE LIMITED   ' || city
                END
              WHEN bucket <= 84 THEN
                CASE h % 3
                  WHEN 0 THEN 'IMPS OW/' || (500000000000 + h)::VARCHAR || '/' || person
                           || '/' || ifsc_bank || '/' || (43292707719 + h)::VARCHAR
                  WHEN 1 THEN 'NEFT/' || (h % 999999999)::VARCHAR || '/' || ifsc_bank || '/' || person
                  ELSE 'UPI-' || person || '-XXXXXX' || (h % 10000)::VARCHAR
                           || '-' || ifsc_bank || '0002125-' || (500000000000 + h)::VARCHAR
                END
              WHEN bucket <= 90 THEN charge
              WHEN bucket <= 93 THEN
                'FT - SELF - ' || (50200013729069 + h)::VARCHAR || ' - SELF TRANSFER'
              ELSE
                -- genuinely opaque narration: keeps coverage honest
                CASE h % 3
                  WHEN 0 THEN 'TRF/' || (h * 31)::VARCHAR || '/' || (h * 17)::VARCHAR
                  WHEN 1 THEN 'CLG/' || (h % 999999)::VARCHAR
                  ELSE 'MISC ADJ ' || (h % 99999)::VARCHAR
                END
            END                                                        AS description,
            CASE WHEN bucket <= 90
                 THEN round(((h_amt % 90000) + 100) / 10.0, 2)::DECIMAL(15,2)
                 ELSE round(((h_amt % 500000) + 1000) / 10.0, 2)::DECIMAL(15,2)
            END                                                        AS transaction_amount,
            CASE WHEN h % 11 = 0 THEN NULL
                 ELSE 'S' || (10000000 + h)::VARCHAR END               AS transaction_reference_id,
            CASE WHEN h % 3 = 0 THEN NULL
                 ELSE 'enc:' || md5((h * 7919)::VARCHAR) END           AS utr_number
        FROM typed;
    """)

    ext = "parquet" if fmt == "parquet" else "csv"
    written = {}
    for table in ("bank", "account", "transaction"):
        path = os.path.join(out_dir, f"{table}.{ext}").replace("\\", "/")
        if ext == "parquet":
            con.execute(f"COPY {table} TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD);")
        else:
            con.execute(f"COPY {table} TO '{path}' (HEADER, DELIMITER ',');")
        written[table] = (path, con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    elapsed = time.perf_counter() - t0
    span = con.execute(
        "SELECT MIN(transaction_date), MAX(transaction_date) FROM transaction"
    ).fetchone()
    mix = con.execute("""
        SELECT transaction_type, COUNT(*) n FROM transaction GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    con.close()

    print(f"Generated in {elapsed:.1f}s -> {out_dir}")
    for t, (p, n) in written.items():
        print(f"  {t:12} {n:>10,} rows  {os.path.basename(p)}")
    print(f"  date span    {span[0]} .. {span[1]}")
    print(f"  type mix     {dict(mix)}")
    print(f"  anchor date  {span[1].date()}")
    return {"written": written, "elapsed": elapsed, "anchor": span[1]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate V2 synthetic bank data.")
    ap.add_argument("--rows", type=int, default=200_000, help="transaction count")
    ap.add_argument("--entities", type=int, default=50)
    ap.add_argument("--accounts", type=int, default=200)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    generate(rows=args.rows, entities=args.entities, accounts=args.accounts,
             months=args.months, fmt=args.format, out_dir=args.out)
