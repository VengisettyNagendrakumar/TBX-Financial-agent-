"""
Configuration & Schema Mapping (V2 — bank / account / transaction)
==================================================================
SINGLE SOURCE OF TRUTH for the warehouse layer.

The hackathon schema is three tables:
    bank        -- bank_code (PK), bank_name
    account     -- account_id (PK), entity_id, account_number, program_id,
                   available_balance, bank_code (FK)
    transaction -- transaction_id (PK), account_id (FK), transaction_date,
                   transaction_type, description, transaction_amount,
                   transaction_reference_id, utr_number

Everything downstream reads column names from SCHEMA_CONFIG, so swapping in the
organisers' real export means editing this file and nothing else.
"""

import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Persisted analytical warehouse. Built once, reloaded instantly on restart.
WAREHOUSE_PATH = os.path.join(BASE_DIR, "warehouse.duckdb")

# -------------------------------------------------------------
# SOURCE TABLES  (edit column names here when the real export lands)
# -------------------------------------------------------------
TABLE_BANK = "bank"
TABLE_ACCOUNT = "account"
TABLE_TRANSACTION = "transaction"

SCHEMA_CONFIG = {
    "bank": {
        "file": "bank",
        "code_col": "bank_code",
        "name_col": "bank_name",
    },
    "account": {
        "file": "account",
        "id_col": "account_id",
        "entity_col": "entity_id",
        "number_col": "account_number",       # SENSITIVE - mask, never show raw
        "program_col": "program_id",
        "balance_col": "available_balance",
        "bank_code_col": "bank_code",
    },
    "transaction": {
        "file": "transaction",
        "id_col": "transaction_id",
        "account_id_col": "account_id",
        "date_col": "transaction_date",
        "type_col": "transaction_type",
        "desc_col": "description",
        "amount_col": "transaction_amount",
        "ref_col": "transaction_reference_id",  # plaintext, searchable
        "utr_col": "utr_number",                # SENSITIVE - see §6.3 of the plan
    },
}

# -------------------------------------------------------------
# DERIVED WAREHOUSE TABLES  (built by build_warehouse.py)
# -------------------------------------------------------------
TABLE_TXN_FACT = "txn_fact"            # enriched transactions (the evidence)
TABLE_ROLLUP_MONTHLY = "rollup_monthly"  # pre-aggregated (the answers)
TABLE_MERCHANT_DIM = "merchant_dim"    # counterparty vocabulary
TABLE_MERCHANT_ALIAS = "merchant_alias"  # raw string -> canonical
TABLE_MANIFEST = "ingest_manifest"     # watermark + versions

# -------------------------------------------------------------
# TRANSACTION DIRECTION
# The V1 bug where 'Failed' payouts were summed into spend (BUGS.md B02)
# reappears here as direction: never aggregate without an explicit type.
# -------------------------------------------------------------
TXN_DEBIT = "debit"
TXN_CREDIT = "credit"
VALID_TXN_TYPES = (TXN_DEBIT, TXN_CREDIT)

# -------------------------------------------------------------
# COUNTERPARTY CLASSIFICATION
# -------------------------------------------------------------
KIND_MERCHANT = "merchant"
KIND_PERSON = "person"
KIND_BANK_CHARGE = "bank_charge"
KIND_SELF_TRANSFER = "self_transfer"
KIND_UNKNOWN = "unknown"

# Excluded from "which vendor did I spend the most on" - bank fees and
# own-account movements are not vendor spend.
KINDS_EXCLUDED_FROM_SPEND_RANKING = (KIND_BANK_CHARGE, KIND_SELF_TRANSFER, KIND_UNKNOWN)

UNKNOWN_MERCHANT = "UNKNOWN"

# -------------------------------------------------------------
# PAYMENT RAILS  (drive description parsing in enrichment.py)
# -------------------------------------------------------------
CHANNEL_UPI = "UPI"
CHANNEL_NEFT = "NEFT"
CHANNEL_IMPS = "IMPS"
CHANNEL_RTGS = "RTGS"
CHANNEL_FT = "FT"
CHANNEL_CHARGE = "CHARGE"
CHANNEL_OTHER = "OTHER"

# Narration substrings that mark a bank fee rather than a counterparty payment.
BANK_CHARGE_PATTERNS = [
    "charges", "charge", "amc fee", "service fee", "gst on", "sms alert",
    "annual fee", "penalty", "min bal", "atm fee", "processing fee",
]

# Narration fields that are rail keywords or reference codes, never counterparty
# names. Without these the extractor picks 'INET' or a reference code instead of
# the merchant.
NARRATION_STOPWORDS = [
    "UPI", "NEFT", "IMPS", "RTGS", "FT", "P2A", "P2P", "INET", "OW", "IW",
    "INWD", "OUTW", "CLG", "TRF", "MISC", "ADJ", "REF", "SELF", "MOB", "NET",
    "ATM", "POS", "ACH", "ECS", "NACH", "CMS", "BY", "TO", "FROM", "CR", "DR",
    "SELF TRANSFER", "CHQ", "CASH", "TFR", "PAYMENT", "TRANSFER", "INB",
]

# Narration substrings marking an own-account movement rather than a payment to
# a third party. Excluded from vendor spend rankings.
SELF_TRANSFER_PATTERNS = ["self transfer", "- self -", "/self/", "own account", "self a/c"]

# -------------------------------------------------------------
# PERSON vs MERCHANT CLASSIFICATION
# -------------------------------------------------------------
# Words that mark a commercial entity. Used as a negative signal for "person".
CORPORATE_MARKERS = [
    "TECHNOLOGIES", "TECHNOLOGY", "SERVICES", "SOLUTIONS", "SYSTEMS", "ENTERPRISES",
    "INDUSTRIES", "RETAIL", "STORES", "STORE", "MART", "SUPERMARKET", "BAZAAR",
    "ELECTRONICS", "PHARMACY", "PHARMA", "HOSPITAL", "CLINIC", "MEDICAL",
    "DIGITAL", "POWER", "ENERGY", "TELECOM", "COMMUNICATIONS", "MOTORS",
    "TRAVELS", "TOURS", "AIRLINES", "HOTELS", "RESTAURANT", "FOODS", "BEVERAGES",
    "APPAREL", "FASHION", "JEWELLERS", "TRADERS", "AGENCIES", "DISTRIBUTORS",
    "CONSULTANCY", "CONSULTING", "CAPITAL", "FINANCE", "FINSERV", "INSURANCE",
    "BANK", "PAYMENTS", "COMMERCE", "ONLINE", "INTERNET", "SOFTWARE", "LABS",
    "FIT", "GYM", "SALON", "CAFE", "KITCHEN", "SELECTION", "COMPANY", "GROUP",
]

# Scoring thresholds for the person classifier (enrichment.py).
PERSON_SCORE_THRESHOLD = 3
PERSON_MAX_TOKENS = 4

# -------------------------------------------------------------
# MERCHANT NORMALISATION
# -------------------------------------------------------------
# Stripped from the tail of an extracted counterparty name.
LEGAL_SUFFIXES = [
    "PRIVATE LIMITED", "PVT LTD", "PVT. LTD.", "PVT LTD.", "P LTD",
    "LIMITED", "LTD", "LLP", "LLC", "INC", "CORP", "CORPORATION",
    "COMPANY", "CO", "AND CO", "INDIA", "INDIA PVT",
]

# Brand <-> legal-entity mapping. Without this, Swiggy spend splits across two
# canonicals and every historical total is wrong.
# NOTE: changing this map retroactively changes past answers - bump
# ALIAS_MAP_VERSION so the warehouse knows to re-map (see plan §12.3).
MERCHANT_ALIASES = {
    "BUNDL TECHNOLOGIES": "SWIGGY",
    "SWIGGY INSTAMART": "SWIGGY",
    "SWIGGYIT": "SWIGGY",
    "ETERNAL": "ZOMATO",
    "ZOMATO MEDIA": "ZOMATO",
    "BLINK COMMERCE": "BLINKIT",
    "GROFERS INDIA": "BLINKIT",
    "AMAZON PAY": "AMAZON",
    "AMAZON SELLER SERVICES": "AMAZON",
    "AMZN": "AMAZON",
    "FLIPKART INTERNET": "FLIPKART",
    "UBER INDIA SYSTEMS": "UBER",
    "OLA CABS": "OLA",
    "ANI TECHNOLOGIES": "OLA",
}

ALIAS_MAP_VERSION = 1

# Fuzzy-clustering threshold used when unifying spelling variants of the same
# merchant. Applied to the DISTINCT vocabulary (~3k strings), never to rows.
MERCHANT_CLUSTER_THRESHOLD = 92

# -------------------------------------------------------------
# INGEST
# -------------------------------------------------------------
# Banking systems post late: a transaction dated the 3rd can arrive on the 9th.
# Incremental loads re-scan this many days behind the watermark and dedupe on
# the primary key. See plan §12.3.
INGEST_LOOKBACK_DAYS = 7

# -------------------------------------------------------------
# SECURITY
# -------------------------------------------------------------
# TLS to MySQL. Never disable verification.
MYSQL_DSN = {
    "host": os.getenv("MYSQL_HOST", ""),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", ""),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", ""),
    "ssl_ca": os.getenv("MYSQL_SSL_CA", ""),
    "ssl_verify_identity": True,
}

# Pepper for the UTR blind index (HMAC-SHA256) so encrypted UTRs remain
# equality-searchable without decrypting every row. Absent -> UTR search is
# disabled and returns an honest guardrail rather than a wrong answer.
UTR_BLIND_INDEX_PEPPER = os.getenv("UTR_BLIND_INDEX_PEPPER", "")

# Digit runs at least this long are redacted before any text reaches the LLM.
PII_MIN_DIGIT_RUN = 9
ACCOUNT_NUMBER_VISIBLE_SUFFIX = 4

# -------------------------------------------------------------
# MODEL  (Section 7: <= 20B parameters; must support tool calling)
# -------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ACTIVE_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

# Candidates for the model-efficiency benchmark (BUGS.md B16).
BENCHMARK_MODELS = [
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
    "openai/gpt-oss-20b",
]

CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "₹")
