"""
Merchant Enrichment (Phase 1)
=============================
Recreates the vendor dimension the new schema does not provide.

The V1 schema had a `vendors` table to fuzzy-match against. The V2 schema has
none: "Swiggy" exists only inside free-text narration. Without a derived
grouping key, "which vendor did I spend the most on" is unanswerable and
`LIKE '%swiggy%'` can never be indexed.

Three passes:

  A. EXTRACT   (SQL, vectorised)  -- rail detection + counterparty extraction
                                     over every row. ~2.4s at 4M rows.
  B. NORMALISE (Python, on the DISTINCT vocabulary only) -- suffix stripping,
                                     alias folding, fuzzy clustering, and
                                     person/merchant classification.
  C. MAP       (SQL join)         -- raw string -> canonical, onto every row.

Pass B is the reason this scales. Measured on 4M rows there are ~3,000 distinct
merchant strings (1 per 1,333 rows), so clustering runs over thousands of items,
never millions. Clustering 4M strings pairwise is not computationally feasible.

Coverage is reported, never assumed. Rows whose counterparty cannot be
extracted become UNKNOWN and are surfaced as unattributed spend rather than
silently dropped -- the same grounding discipline V1 applied to numbers.
"""

import re
import time
from collections import defaultdict

from rapidfuzz import fuzz, process

import config


# =============================================================
# PASS A — extraction (SQL)
# =============================================================

def _sql_list(values) -> str:
    return "[" + ", ".join("'" + str(v).replace("'", "''") + "'" for v in values) + "]"


def build_extraction_sql(source: str = "raw_transaction", extra_stopwords=None) -> str:
    """
    Returns SQL projecting channel, counterparty candidate and month.

    Counterparty extraction is deliberately generic rather than a table of
    per-rail field positions. Positions drift between banks (the sample's
    IMPS/P2A name sits at field 9, IMPS OW at field 3), so instead we split on
    the dominant delimiter and pick the most name-like field: purely alphabetic,
    at least 3 characters, not a rail keyword, not a masked-account run of X's.
    Reference codes such as ZBFLCTP5L2PBL2933 and INWD48 are excluded because
    they contain digits.

    `extra_stopwords` carries the IFSC bank codes read from the bank table.
    They are four alphabetic characters and so pass the name filter, which
    means they beat any shorter brand on the longest-field rule: measured on 4M
    rows, 46,348 transactions (1.16%) had their counterparty read as 'HDFC' or
    'SBIN' instead of OLA, UBER or JIO. Stopping them is what keeps short brand
    names from silently understating.
    """
    t = config.SCHEMA_CONFIG["transaction"]
    desc, date_col = t["desc_col"], t["date_col"]
    stop = _sql_list(list(config.NARRATION_STOPWORDS) +
                     [str(s).upper() for s in (extra_stopwords or [])])
    charge = " OR ".join(
        f"lower({desc}) LIKE '%{p.lower()}%'" for p in config.BANK_CHARGE_PATTERNS
    )
    selft = " OR ".join(
        f"lower({desc}) LIKE '%{p.lower()}%'" for p in config.SELF_TRANSFER_PATTERNS
    )

    return f"""
    WITH split AS (
        SELECT *,
            CASE
              WHEN contains({desc}, '/')   THEN str_split({desc}, '/')
              WHEN contains({desc}, ' - ') THEN str_split({desc}, ' - ')
              WHEN contains({desc}, '-')   THEN str_split({desc}, '-')
              ELSE [{desc}]
            END AS parts
        FROM {source}
    ),
    cand AS (
        SELECT *,
            list_filter(
                list_transform(parts, x -> trim(x)),
                x -> regexp_matches(x, '^[A-Za-z][A-Za-z .&''-]{{2,}}$')
                     AND NOT list_contains({stop}, upper(x))
                     AND NOT regexp_matches(upper(x), '^X+$')
            ) AS names
        FROM split
    )
    SELECT
        * EXCLUDE (parts, names),
        CASE
          WHEN {charge} THEN '{config.CHANNEL_CHARGE}'
          WHEN {desc} LIKE 'UPI%'  THEN '{config.CHANNEL_UPI}'
          WHEN {desc} LIKE 'IMPS%' THEN '{config.CHANNEL_IMPS}'
          WHEN {desc} LIKE 'NEFT%' THEN '{config.CHANNEL_NEFT}'
          WHEN {desc} LIKE 'RTGS%' OR {desc} LIKE 'R/%' THEN '{config.CHANNEL_RTGS}'
          WHEN {desc} LIKE 'FT %'  OR {desc} LIKE 'FT-%' THEN '{config.CHANNEL_FT}'
          ELSE '{config.CHANNEL_OTHER}'
        END AS channel,
        CASE
          WHEN {charge} THEN '__BANK_CHARGE__'
          WHEN {selft}  THEN '__SELF_TRANSFER__'
          WHEN len(names) = 0 THEN NULL
          ELSE
            -- longest surviving field, then drop any trailing location that a
            -- 2+ space run separates ('SELECTION ELECTRONICS   DAHISAR EAST')
            trim(split_part(
                list_reduce(names,
                    (a, x) -> CASE WHEN length(x) > length(a) THEN x ELSE a END),
                '  ', 1))
        END AS merchant_raw,
        date_trunc('month', {date_col}) AS txn_month
    FROM cand
    """


# =============================================================
# PASS B — vocabulary normalisation (Python, distinct strings only)
# =============================================================

# Pass A emits these markers instead of a counterparty name. They must survive
# normalisation untouched so the classifier can recognise them.
SENTINEL_BANK_CHARGE = "__BANK_CHARGE__"
SENTINEL_SELF_TRANSFER = "__SELF_TRANSFER__"
_SENTINELS = (SENTINEL_BANK_CHARGE, SENTINEL_SELF_TRANSFER)
_SENTINEL_LABELS = {
    SENTINEL_BANK_CHARGE: "BANK CHARGES",
    SENTINEL_SELF_TRANSFER: "SELF TRANSFER",
}

_SUFFIX_RE = re.compile(
    r"[\s,.]*\b(" + "|".join(re.escape(s) for s in sorted(
        config.LEGAL_SUFFIXES, key=len, reverse=True)) + r")\s*$",
    re.IGNORECASE,
)
_TRAILING_CODE_RE = re.compile(r"\s+[A-Z]{0,4}\d[A-Z0-9]*$")


def normalise_name(raw: str) -> str:
    """
    Canonical form of one counterparty string.

    'Zomato Private Limited' / 'ZOMATO LTD' / 'zomato' all fold to 'ZOMATO'.
    Legal suffixes are stripped repeatedly because real narration stacks them
    ('FOO INDIA PVT LTD').
    """
    if not raw:
        return ""
    s = str(raw).upper().strip()
    s = re.sub(r"[^A-Z0-9 &.'-]", " ", s)
    s = _TRAILING_CODE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(3):
        stripped = _SUFFIX_RE.sub("", s).strip(" ,.")
        if stripped == s or not stripped:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip()


def classify_kind(name: str, raw_samples: list, credit_ratio: float, freq: int) -> tuple:
    """
    Decides merchant vs person for one canonical name.

    Casing alone is unreliable -- the provided sample contains both
    'Gautam singh' and 'PARESH VIKRANT GHASE'. So this scores several weak
    signals instead of trusting one, and returns the score so the confidence is
    visible rather than implied.
    """
    if name == SENTINEL_BANK_CHARGE:
        return config.KIND_BANK_CHARGE, 1.0
    if name == SENTINEL_SELF_TRANSFER:
        return config.KIND_SELF_TRANSFER, 1.0
    if not name or name == config.UNKNOWN_MERCHANT:
        return config.KIND_UNKNOWN, 1.0

    tokens = name.split()
    score = 0

    # Corporate vocabulary is the strongest negative signal.
    if any(m in tokens for m in config.CORPORATE_MARKERS):
        score -= 3
    # Single-token names are overwhelmingly brands (SWIGGY, ZOMATO, DMART).
    if len(tokens) == 1:
        score -= 2
    elif 2 <= len(tokens) <= config.PERSON_MAX_TOKENS and all(t.isalpha() for t in tokens):
        score += 1
    # Mixed case survived in the raw narration -> bank preserved a human name.
    if any(s != s.upper() and s != s.lower() for s in raw_samples[:5]):
        score += 2
    # People mostly send money in; merchants mostly take it out.
    if credit_ratio > 0.6:
        score += 2
    elif credit_ratio < 0.2:
        score -= 1
    # Merchants recur far more often than individuals.
    if freq <= 3:
        score += 1

    kind = config.KIND_PERSON if score >= config.PERSON_SCORE_THRESHOLD else config.KIND_MERCHANT
    confidence = min(1.0, 0.5 + abs(score) / 8.0)
    return kind, round(confidence, 2)


def build_alias_map(vocab_rows: list) -> list:
    """
    Folds the distinct vocabulary onto canonical merchants.

    vocab_rows: [(merchant_raw, freq, credit_ratio, sample_raw)]
    Returns:    [(merchant_raw, canonical, kind, kind_confidence)]

    Order matters. Explicit aliases win over fuzzy clustering, because
    BUNDL TECHNOLOGIES -> SWIGGY is a fact about the world that no string
    similarity can discover.
    """
    normalised = {}
    meta = defaultdict(lambda: {"freq": 0, "credits": 0.0, "samples": []})

    for raw, freq, credit_ratio, sample in vocab_rows:
        # Sentinels must bypass normalise_name, which strips underscores and
        # would turn __BANK_CHARGE__ into an ordinary-looking 'BANK CHARGE'
        # that then scores as a merchant and pollutes spend rankings.
        if raw in _SENTINELS:
            norm = raw
        else:
            norm = normalise_name(raw)
            if not norm:
                norm = config.UNKNOWN_MERCHANT
            norm = config.MERCHANT_ALIASES.get(norm, norm)
        normalised[raw] = norm
        m = meta[norm]
        m["freq"] += freq
        m["credits"] += (credit_ratio or 0.0) * freq
        if sample:
            m["samples"].append(sample)

    # Fuzzy-cluster the remaining variants. Anchors are the highest-frequency
    # names, so rare misspellings collapse onto the common spelling.
    anchors = sorted(
        (n for n in meta if n not in _SENTINELS and n != config.UNKNOWN_MERCHANT),
        key=lambda n: -meta[n]["freq"],
    )
    canonical_of = {}
    accepted = []
    for name in anchors:
        if len(name) < 4:
            canonical_of[name] = name
            accepted.append(name)
            continue
        hit = process.extractOne(
            name, accepted, scorer=fuzz.token_set_ratio,
            score_cutoff=config.MERCHANT_CLUSTER_THRESHOLD,
        )
        if hit:
            canonical_of[name] = canonical_of.get(hit[0], hit[0])
        else:
            canonical_of[name] = name
            accepted.append(name)

    for special in (config.UNKNOWN_MERCHANT, *_SENTINELS):
        canonical_of.setdefault(special, special)

    # Re-aggregate onto the final canonicals before classifying.
    final = defaultdict(lambda: {"freq": 0, "credits": 0.0, "samples": []})
    for norm, m in meta.items():
        c = canonical_of.get(norm, norm)
        f = final[c]
        f["freq"] += m["freq"]
        f["credits"] += m["credits"]
        f["samples"].extend(m["samples"][:5])

    kinds = {}
    for canonical, m in final.items():
        ratio = (m["credits"] / m["freq"]) if m["freq"] else 0.0
        kinds[canonical] = classify_kind(canonical, m["samples"], ratio, m["freq"])

    out = []
    for raw, norm in normalised.items():
        canonical = canonical_of.get(norm, norm)
        # Look the kind up under the sentinel, then present a readable label.
        kind, conf = kinds.get(canonical, (config.KIND_MERCHANT, 0.5))
        out.append((raw, _SENTINEL_LABELS.get(canonical, canonical), kind, conf))
    return out


# =============================================================
# ORCHESTRATION
# =============================================================

def enrich(con, source: str = "raw_transaction", verbose: bool = True) -> dict:
    """
    Runs passes A-C and materialises txn_fact, merchant_alias and merchant_dim.
    Returns timing and coverage statistics.
    """
    acct = config.SCHEMA_CONFIG["account"]
    txn = config.SCHEMA_CONFIG["transaction"]
    stats = {}

    # ---- Pass A -------------------------------------------------------
    t0 = time.perf_counter()
    try:
        bank_codes = [r[0] for r in con.execute(
            f"SELECT DISTINCT {config.SCHEMA_CONFIG['bank']['code_col']} FROM raw_bank"
        ).fetchall() if r[0]]
    except Exception:
        bank_codes = []
    con.execute(
        f"CREATE OR REPLACE TABLE _extracted AS "
        f"{build_extraction_sql(source, extra_stopwords=bank_codes)};"
    )
    stats["extract_s"] = time.perf_counter() - t0
    stats["bank_codes_stopped"] = len(bank_codes)

    # ---- Pass B -------------------------------------------------------
    t0 = time.perf_counter()
    vocab = con.execute(f"""
        SELECT merchant_raw,
               COUNT(*)                                                   AS freq,
               AVG(CASE WHEN {txn['type_col']} = '{config.TXN_CREDIT}'
                        THEN 1.0 ELSE 0.0 END)                            AS credit_ratio,
               ANY_VALUE(merchant_raw)                                    AS sample_raw
        FROM _extracted
        WHERE merchant_raw IS NOT NULL
        GROUP BY merchant_raw
    """).fetchall()
    alias_rows = build_alias_map(vocab)
    stats["distinct_raw"] = len(vocab)
    stats["distinct_canonical"] = len({r[1] for r in alias_rows})

    con.execute(f"""
        CREATE OR REPLACE TABLE {config.TABLE_MERCHANT_ALIAS} (
            merchant_raw VARCHAR, merchant_norm VARCHAR,
            counterparty_kind VARCHAR, kind_confidence DOUBLE
        );
    """)
    if alias_rows:
        con.executemany(
            f"INSERT INTO {config.TABLE_MERCHANT_ALIAS} VALUES (?, ?, ?, ?);", alias_rows
        )
    stats["normalise_s"] = time.perf_counter() - t0

    # ---- Pass C -------------------------------------------------------
    t0 = time.perf_counter()
    con.execute(f"""
        CREATE OR REPLACE TABLE {config.TABLE_TXN_FACT} AS
        SELECT
            e.{txn['id_col']}, e.{txn['account_id_col']}, a.{acct['entity_col']} AS entity_id,
            e.{txn['date_col']}, e.txn_month, e.{txn['type_col']},
            e.{txn['amount_col']}, e.{txn['desc_col']}, e.channel,
            e.{txn['ref_col']}, e.{txn['utr_col']},
            COALESCE(m.merchant_norm, '{config.UNKNOWN_MERCHANT}')      AS merchant_norm,
            COALESCE(m.counterparty_kind, '{config.KIND_UNKNOWN}')      AS counterparty_kind,
            COALESCE(m.kind_confidence, 0.0)                            AS kind_confidence
        FROM _extracted e
        LEFT JOIN {config.TABLE_MERCHANT_ALIAS} m ON e.merchant_raw = m.merchant_raw
        LEFT JOIN raw_account a ON e.{txn['account_id_col']} = a.{acct['id_col']}
        ORDER BY entity_id, merchant_norm, {txn['date_col']};
    """)
    con.execute("DROP TABLE IF EXISTS _extracted;")
    stats["map_s"] = time.perf_counter() - t0

    build_merchant_dim(con)

    # ---- Coverage -----------------------------------------------------
    cov = con.execute(f"""
        SELECT
            COUNT(*)                                                          AS total,
            COUNT(*) FILTER (WHERE merchant_norm <> '{config.UNKNOWN_MERCHANT}') AS attributed,
            SUM({txn['amount_col']})                                          AS total_amt,
            SUM({txn['amount_col']}) FILTER
                (WHERE merchant_norm <> '{config.UNKNOWN_MERCHANT}')          AS attributed_amt
        FROM {config.TABLE_TXN_FACT}
    """).fetchone()
    stats["rows"] = cov[0]
    stats["coverage_rows"] = (cov[1] / cov[0]) if cov[0] else 0.0
    stats["coverage_amount"] = (float(cov[3]) / float(cov[2])) if cov[2] else 0.0

    if verbose:
        print(f"  Pass A extract    {stats['extract_s']:.2f}s")
        print(f"  Pass B normalise  {stats['normalise_s']:.2f}s "
              f"({stats['distinct_raw']:,} raw -> {stats['distinct_canonical']:,} canonical)")
        print(f"  Pass C map+sort   {stats['map_s']:.2f}s")
        print(f"  Coverage          {stats['coverage_rows']:.1%} of rows, "
              f"{stats['coverage_amount']:.1%} of value")
    return stats


def build_merchant_dim(con):
    """The counterparty vocabulary the resolver searches (replaces `vendors`)."""
    txn = config.SCHEMA_CONFIG["transaction"]
    con.execute(f"""
        CREATE OR REPLACE TABLE {config.TABLE_MERCHANT_DIM} AS
        SELECT
            entity_id, merchant_norm,
            ANY_VALUE(counterparty_kind)                    AS counterparty_kind,
            MAX(kind_confidence)                            AS kind_confidence,
            COUNT(*)                                        AS txn_count,
            SUM({txn['amount_col']}) FILTER
                (WHERE {txn['type_col']} = '{config.TXN_DEBIT}')  AS total_debit,
            SUM({txn['amount_col']}) FILTER
                (WHERE {txn['type_col']} = '{config.TXN_CREDIT}') AS total_credit,
            MIN(txn_month)                                  AS first_month,
            MAX(txn_month)                                  AS last_month,
            COUNT(DISTINCT txn_month)                       AS active_months
        FROM {config.TABLE_TXN_FACT}
        GROUP BY entity_id, merchant_norm;
    """)


def build_rollup(con):
    """
    Monthly pre-aggregate. Measured at 4M rows this is ~18k rows and rebuilds in
    ~0.16s -- faster than an incremental delta append, so it is always rebuilt
    wholesale rather than upserted (plan §12.3). It cannot drift.
    """
    txn = config.SCHEMA_CONFIG["transaction"]
    con.execute(f"""
        CREATE OR REPLACE TABLE {config.TABLE_ROLLUP_MONTHLY} AS
        SELECT
            entity_id, merchant_norm, counterparty_kind, txn_month,
            {txn['type_col']}                AS transaction_type,
            SUM({txn['amount_col']})         AS total_amount,
            COUNT(*)                         AS txn_count,
            AVG({txn['amount_col']})         AS avg_amount,
            MAX({txn['amount_col']})         AS max_amount
        FROM {config.TABLE_TXN_FACT}
        GROUP BY entity_id, merchant_norm, counterparty_kind, txn_month, {txn['type_col']}
        -- Physical sort order is what makes this fast. Every query filters on
        -- entity_id first, so clustering by it lets DuckDB's zone maps skip
        -- whole row groups. Without the ORDER BY, each lookup scans the full
        -- rollup; an ART index does not help here because these are range
        -- scans, not point lookups.
        ORDER BY entity_id, merchant_norm, txn_month;
    """)
