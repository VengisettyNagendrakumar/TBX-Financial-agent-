# Known Bugs & Issues

Audit of the Grounded Financial Assistant against the TBX — BVP Tech Catalyst
problem statement. Every issue below was reproduced against the committed
dataset in `data/` (anchor date `2024-05-31`, 12 vendors, 71 payouts,
131 transactions).

**How to run any repro in this document**

All snippets assume you are in the repo root with dependencies installed:

```bash
pip install -r requirements.txt
```

Then paste the snippet into a file (e.g. `repro.py`) and run `python repro.py`,
or pipe it straight into the interpreter. Snippets do **not** require a
`GROQ_API_KEY` unless the issue explicitly says so — they exercise the
deterministic layer directly.

---

## Severity index

| ID | Severity | Area | Title |
|----|----------|------|-------|
| [B01](#b01) | 🔴 Critical | Grounding | Unrecognised relative period silently widens query to all-time |
| [B02](#b02) | 🔴 Critical | Accuracy | Failed and Pending payouts are counted as spend |
| [B03](#b03) | 🔴 Critical | Grounding | No grand total when no vendor is named — the LLM must do the arithmetic |
| [B04](#b04) | 🟠 High | Accuracy | `LIMIT 20` silently truncates the vendor breakdown |
| [B05](#b05) | 🟠 High | Grounding | Explainer only sees `df.head(10)` but describes the whole result |
| [B06](#b06) | 🟠 High | NLU | Legal-suffix variants fail to resolve (`Stripe Inc` → NOT_FOUND) |
| [B07](#b07) | 🟠 High | NLU | Category filter has no guardrail — typos silently return zero rows |
| [B08](#b08) | 🟠 High | Functionality | No comparison intent — "how does that compare to last month?" is unsupported |
| [B09](#b09) | 🟠 High | Validation | LLM output is never validated; unknown intent silently becomes `transaction_list` |
| [B10](#b10) | 🟠 High | Testing | Test suite asserts shape, not values — a wrong number passes |
| [B11](#b11) | 🟡 Medium | Validation | Absolute dates unvalidated: raw DuckDB exception or silent empty result |
| [B12](#b12) | 🟡 Medium | Coverage | `chart_of_accounts` is loaded but never queried; incidental transactions invisible |
| [B13](#b13) | 🟡 Medium | Multi-turn | Current question is duplicated into its own chat history |
| [B14](#b14) | 🟡 Medium | UX | Duplicate `st.download_button` key crashes on repeated answers |
| [B15](#b15) | 🟡 Medium | Concurrency | Single DuckDB connection shared across Streamlit reruns |
| [B16](#b16) | 🟡 Medium | Model choice | Ships the largest permitted model with no smaller-model benchmark |
| [B17](#b17) | 🟢 Low | Docs | Stale `gpt-oss-120b` references contradict the Section 7 compliance claim |
| [B18](#b18) | 🟢 Low | NLU | Fallback parser never populates `category` |
| [B19](#b19) | 🟢 Low | Testing | Injection test uses a non-existent intent name |
| [B20](#b20) | 🟢 Low | Claims | 20M-record and `<5ms` claims are unmeasured |

---

<a id="b01"></a>
## B01 — Unrecognised relative period silently widens the query to all-time

**Severity:** 🔴 Critical &nbsp;|&nbsp; **Files:** [db.py](db.py) `calculate_relative_date_range`, [query_builder.py](query_builder.py) `build_sql`

### What's wrong

`calculate_relative_date_range` matches the period against hardcoded string
lists (`period in ["last_month", "previous_month"]`). Anything it does not
recognise returns `(None, None)`.

`build_sql` then treats `(None, None)` as *"no date filter requested"* and omits
the `BETWEEN` clause entirely — so the query silently expands to the full
history instead of failing.

The root cause is that `(None, None)` is **overloaded**. It means both:
- "the user asked no time question" → correct to query all history, and
- "the user asked a time question I could not parse" → must not answer.

These need to be different signals.

### Why it triggers in practice

`relative_value` never holds raw user text — it is produced by one of two
places ([intent_parser.py:113-126](intent_parser.py:113) fallback parser, or the
LLM). A closed vocabulary is the right design here. The problem is the LLM
side: `response_format={"type": "json_object"}` guarantees *syntactically valid
JSON only*. It does **not** constrain values to the enum in the prompt at
[intent_parser.py:37](intent_parser.py:37), and nothing in the codebase
validates the returned value. An unusual phrasing ("what did we spend in the
trailing month?") can produce `"trailing_month"` or `"past_month"`, which
`db.py` has never heard of.

The fallback parser has the same shape of gap one layer up: "last 3 months",
"since January", "trailing 30 days", "H1", "last year" match none of its
keyword branches, fall through the month-name loop, and land on
`type: "all"` — again, whole history, silently.

### Reproduce

```python
from db import calculate_relative_date_range
from datetime import date

anchor = date(2024, 5, 31)
for p in ["last_month", "previous_month", "LAST_MONTH", " last_month ",
          "last month", "last-month", "lastMonth",
          "past_month", "prior_month", "mtd", "qtd", "last_year", "last_3_months"]:
    print(f"{p!r:18} -> {calculate_relative_date_range(p, anchor)}")
```

Observed:

```
'last_month'       -> ('2024-04-01', '2024-04-30')
'previous_month'   -> ('2024-04-01', '2024-04-30')
'LAST_MONTH'       -> ('2024-04-01', '2024-04-30')
' last_month '     -> ('2024-04-01', '2024-04-30')
'last month'       -> (None, None)      <-- space instead of underscore
'last-month'       -> (None, None)
'lastMonth'        -> (None, None)
'past_month'       -> (None, None)
'prior_month'      -> (None, None)
'mtd'              -> (None, None)
'qtd'              -> (None, None)
'last_year'        -> (None, None)
'last_3_months'    -> (None, None)
```

Now the downstream damage:

```python
from query_builder import build_sql
from db import get_db_connection, get_anchor_date

con = get_db_connection()
anchor = get_anchor_date(con)

def run(rv):
    intent = {"intent": "spend_summary",
              "date_filter": {"type": "relative", "relative_value": rv},
              "reconciliation_status": "all", "category": None}
    qi = build_sql(intent, "Acme Corporation", anchor)
    df = con.execute(qi["sql"], qi["params"]).df()
    total = float(df["total_spend"].iloc[0]) if len(df) else 0.0
    print(f"{rv!r:14} window={qi['start_date']}..{qi['end_date']} total=${total:,.2f}")

run("last_month")   # recognised
run("past_month")   # not recognised
run("mtd")          # not recognised
```

Observed:

```
'last_month'   window=2024-04-01..2024-04-30 total=$6,050.91
'past_month'   window=None..None             total=$96,892.87
'mtd'          window=None..None             total=$96,892.87
```

**The answer is 16× too large, with no error, no warning, and a "High
Certainty" badge** — because confidence in [pipeline.py](pipeline.py) is derived
purely from vendor-match quality and never inspects date resolution.

Note also an asymmetry: `q1`–`q4` *are* handled in `db.py` but are **absent
from the LLM's schema enum** at [intent_parser.py:37](intent_parser.py:37).
"What did we spend in Q1" works today only because the model happens to guess
`"q1"` or fall back to absolute dates.

### How to test the fix

Add to `test_suite.py`:

```python
# 1. Unknown periods must NOT silently produce an all-history answer.
for bad in ["past_month", "mtd", "trailing_month", "last_3_months"]:
    res = pipeline.process_query_with_period(bad)   # or drive via build_sql
    assert res["status"] == "UNRESOLVED_PERIOD", f"{bad} silently widened scope"

# 2. Formatting variants must all resolve identically.
from db import calculate_relative_date_range
from datetime import date
anchor = date(2024, 5, 31)
expected = ("2024-04-01", "2024-04-30")
for variant in ["last_month", "Last Month", "last month", "last-month", "LAST_MONTH"]:
    assert calculate_relative_date_range(variant, anchor) == expected, variant

# 3. A genuinely absent date filter still means all-history.
assert calculate_relative_date_range(None, anchor) == (None, None)
```

### Fix

1. **Distinguish "not requested" from "not understood."** Return a sentinel
   (or raise `UnresolvedPeriodError`) when a period was supplied but could not
   be parsed. Have `pipeline.process_query` catch it and emit a clarifying
   guardrail response in the same style as the existing `AMBIGUOUS` path:
   *"I couldn't resolve the time period 'trailing month'. Did you mean last
   month (April 2024)?"* This converts a silent wrong number into a question —
   exactly what the problem statement's hallucination-guardrail requirement asks
   for.
2. **Normalise before matching** — kills the whole `"last month"` /
   `"last-month"` / `"Last Month"` class in two lines:
   ```python
   period = period.lower().strip().replace(" ", "_").replace("-", "_")
   ```
3. **Replace the if/elif chain with an alias → canonical dict** so the
   vocabulary lives in one place, and add the common synonyms (`past_month`,
   `prior_month`, `mtd`, `qtd`, `last_year`). Better still, support a
   parameterised `last_n_months` rather than enumerating each offset.
4. **Add `q1`–`q4` to the LLM schema enum** so the two vocabularies match.
5. **Surface the resolved window in the UI** ("Interpreted as: April 1–30,
   2024"). No vocabulary will ever be complete, so make misses *visible*
   instead of silent. Also strengthens the explainability requirement.

---

<a id="b02"></a>
## B02 — Failed and Pending payouts are counted as spend

**Severity:** 🔴 Critical &nbsp;|&nbsp; **File:** [query_builder.py](query_builder.py) `build_sql`

### What's wrong

`build_sql` never filters `vendor_payouts.status`. Every `spend_summary`,
`transaction_list` and `category_summary` sums **Completed, Pending and Failed
payouts together**. The intent schema has no field for payout status either, so
there is no way for a user to ask for the correct figure.

Reporting a *failed* payout as money spent is a finance correctness error, not a
rounding issue.

### Reproduce

```python
import duckdb, config, os

con = duckdb.connect(":memory:")
for t, m in config.SCHEMA_CONFIG.items():
    p = os.path.join(config.DATA_DIR, m["file"]).replace("\\", "/")
    con.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM read_csv_auto('{p}');")

print(con.execute("""
    SELECT status, COUNT(*) AS n, ROUND(SUM(amount), 2) AS total
    FROM vendor_payouts GROUP BY status ORDER BY total DESC
""").df())

print("May, all statuses :", con.execute(
    "SELECT ROUND(SUM(amount),2) FROM vendor_payouts "
    "WHERE payout_date BETWEEN '2024-05-01' AND '2024-05-31'").fetchone())
print("May, Completed only:", con.execute(
    "SELECT ROUND(SUM(amount),2) FROM vendor_payouts "
    "WHERE payout_date BETWEEN '2024-05-01' AND '2024-05-31' "
    "AND status='Completed'").fetchone())
```

Observed:

```
      status   n      total
0  Completed  62  465691.47
1    Pending   8   43970.63
2     Failed   1    6803.90

May, all statuses  : (173536.41,)
May, Completed only: (154294.93,)
```

Ask *"How much did we spend in May?"* and the assistant answers **$173,536.41**,
which includes a **failed** payout of $6,803.90 and $43,970.63 that has not
settled. The defensible answer is $154,294.93.

### How to test the fix

```python
# Default "spend" must exclude Failed payouts.
res = pipeline.process_query("How much did we spend in May 2024?")
total = float(res["table"]["total_spend"].sum())
assert abs(total - 154294.93) < 0.01, f"got {total}, expected Completed-only total"

# The SQL must carry an explicit status predicate.
assert "status" in res["sql"].lower()

# Pending must still be reachable, not silently dropped.
res2 = pipeline.process_query("Show pending payouts in May 2024")
assert not res2["table"].empty
```

### Fix

1. Add `payout_status` to the intent JSON schema (`completed` | `pending` |
   `failed` | `all`), defaulting to `completed` for spend-type intents.
2. Add the predicate in `build_sql` as a bound `?` parameter, consistent with
   the existing filters.
3. Return the status split alongside the total so the explainer can say
   *"$154,294.93 settled, plus $43,970.63 pending"* — more useful than either
   number alone, and it demonstrates real finance domain handling to judges.

---

<a id="b03"></a>
## B03 — No grand total when no vendor is named; the LLM must do the arithmetic

**Severity:** 🔴 Critical &nbsp;|&nbsp; **Files:** [query_builder.py:120-137](query_builder.py:120), [explainer.py:40](explainer.py:40)

### What's wrong

For a question with no specific vendor — *"How much did we spend on vendor
payouts last month?"*, which is **example #1 in the problem statement** — the
`spend_summary` branch returns one row *per vendor* with `GROUP BY v.vendor_name`
and **no grand-total row**.

`generate_explanation` then hands those rows to the LLM with the instruction
*"DO NOT recalculate, aggregate, or guess any figures."* The model is therefore
asked a total question, given only components, and told not to add them. It
either refuses to answer the question asked, or it sums 12 numbers in its head.

That is precisely the hallucination the whole architecture exists to prevent,
sitting on the single most likely demo question.

### Reproduce

```python
from query_builder import build_sql
from db import get_db_connection, get_anchor_date

con = get_db_connection()
anchor = get_anchor_date(con)

intent = {"intent": "spend_summary",
          "date_filter": {"type": "relative", "relative_value": "this_month"},
          "reconciliation_status": "all", "category": None}
qi = build_sql(intent, None, anchor)          # note: no vendor
df = con.execute(qi["sql"], qi["params"]).df()

print(df.to_string())
print("\nrows:", len(df))
print("columns:", list(df.columns))
print("grand total present in result set:", "grand_total" in df.columns)
```

Observed — 12 component rows, no total anywhere:

```
                  vendor_name  total_payouts  total_spend  average_payout
0            Acme Corporation              3     71468.17       23822.72
1   Amazon Web Services, Inc.              2     25594.33       12797.17
2             Stripe Payments              2     18509.70        9254.85
...
11         Slack Technologies              2      2667.91        1333.96

rows: 12
grand total present in result set: False
```

To see the end-to-end effect, set a `GROQ_API_KEY` and run:

```bash
python -c "from pipeline import FinanceAssistantPipeline as P; \
print(P().process_query('How much did we spend in total this month?')['answer'])"
```

The returned prose contains a total figure that appears in **no** column of the
result table — it was produced by the language model, not the database.

### How to test the fix

```python
res = pipeline.process_query("How much did we spend in total this month?")

# Every number in the prose must exist in the computed facts.
import re
facts = set(res["table"].select_dtypes("number").round(2).values.flatten().tolist())
facts.add(res["grand_total"])
for m in re.findall(r"[\d,]+\.\d{2}", res["answer"]):
    assert float(m.replace(",", "")) in facts, f"ungrounded figure: {m}"
```

### Fix

1. Run a scalar aggregate alongside the breakdown and return it as
   `grand_total` / `grand_count` in the result dict.
2. Inject it into the explainer prompt as an explicit pre-computed facts block,
   so the model has the total handed to it rather than inferring it.
3. Display it as a `st.metric` above the table in the UI.

See also [B05](#b05) (same root cause, different trigger) and the numeric
verification proposal at the end of this document.

---

<a id="b04"></a>
## B04 — `LIMIT 20` silently truncates the vendor breakdown

**Severity:** 🟠 High &nbsp;|&nbsp; **File:** [query_builder.py:136](query_builder.py:136)

### What's wrong

The no-vendor `spend_summary` branch ends with `ORDER BY total_spend DESC
LIMIT 20`. With the committed 12-vendor dataset this is invisible. With the
organisers' real vendor list (plausibly hundreds), any total derived from that
table — by the LLM ([B03](#b03)) or by the fallback's
`df["total_spend"].sum()` in [explainer.py](explainer.py) — silently covers only
the top 20 vendors and understates actual spend.

There is no "showing 20 of N" indicator anywhere in the response.

### Reproduce

The shipped data is too small to trigger it, so pad the vendor list:

```python
import duckdb, config, os

con = duckdb.connect(":memory:")
for t, m in config.SCHEMA_CONFIG.items():
    p = os.path.join(config.DATA_DIR, m["file"]).replace("\\", "/")
    con.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM read_csv_auto('{p}');")

# Add 40 synthetic vendors, each with one $1,000 payout in May.
con.execute("""
INSERT INTO vendors
SELECT 'V9' || i, 'Filler Vendor ' || i, 'Misc' FROM range(1, 41) t(i)
""")
con.execute("""
INSERT INTO vendor_payouts
SELECT 'PAY-9' || i, DATE '2024-05-15', 'V9' || i, 1000.00, 'USD', 'Completed', 'filler'
FROM range(1, 41) t(i)
""")

true_total = con.execute("""
    SELECT ROUND(SUM(amount), 2) FROM vendor_payouts
    WHERE payout_date BETWEEN '2024-05-01' AND '2024-05-31'
""").fetchone()[0]

top20 = con.execute("""
    SELECT ROUND(SUM(amount), 2) FROM (
      SELECT v.vendor_name, SUM(p.amount) AS amount
      FROM vendor_payouts p JOIN vendors v ON p.vendor_id = v.vendor_id
      WHERE p.payout_date BETWEEN '2024-05-01' AND '2024-05-31'
      GROUP BY v.vendor_name ORDER BY SUM(p.amount) DESC LIMIT 20
    )
""").fetchone()[0]

print(f"true total      : ${true_total:,.2f}")
print(f"what UI can see : ${top20:,.2f}")
print(f"under-reported  : ${true_total - top20:,.2f}")
```

Observed:

```
true total      : $213,536.41
what UI can see : $181,536.41
under-reported  : $32,000.00
```

32 of the 52 vendors fall outside the `LIMIT 20` window, and their $32,000 of
spend is unreachable by anything downstream.

### How to test the fix

```python
# With >20 vendors, the reported total must equal the true total.
assert res["grand_total"] == true_total
# And truncation must be disclosed.
assert res["truncated"] is True and res["total_vendor_count"] == 52
```

### Fix

Compute the total with a **separate un-limited scalar query** (this is the same
fix as [B03](#b03)) and keep `LIMIT 20` only for the *display* table. Add a
`truncated` flag and render "Showing top 20 of 52 vendors" beneath the
dataframe.

---

<a id="b05"></a>
## B05 — Explainer only sees `df.head(10)` but describes the whole result

**Severity:** 🟠 High &nbsp;|&nbsp; **File:** [explainer.py:40](explainer.py:40)

### What's wrong

```python
Pre-Computed Data Table:
{df.head(10).to_dict(orient='records')}
```

The prompt passes the first 10 rows but the surrounding instructions ask the
model to explain the user's question in full. For a `reconciliation_audit`
(`LIMIT 50`) or a multi-vendor summary, the model characterises a 50-row result
from a 10-row sample — so statements like "most transactions are unreconciled"
or any implied total are extrapolations, not grounded facts.

### Reproduce

```python
from query_builder import build_sql
from db import get_db_connection, get_anchor_date

con = get_db_connection()
anchor = get_anchor_date(con)
qi = build_sql({"intent": "reconciliation_audit",
                "date_filter": {"type": "all"},
                "reconciliation_status": "unreconciled", "category": None},
               None, anchor)
df = con.execute(qi["sql"], qi["params"]).df()

print("rows the DB returned :", len(df))
print("rows the LLM sees    :", len(df.head(10)))
print("sum of visible rows  :", round(df.head(10)["amount"].sum(), 2))
print("sum of ALL rows      :", round(df["amount"].sum(), 2))
```

Observed:

```
rows the DB returned : 27
rows the LLM sees    : 10
sum of visible rows  : 44918.72
sum of ALL rows      : 129905.66
```

The model is asked to characterise 27 unreconciled transactions worth
$129,905.66 while seeing 10 of them worth $44,918.72 — a third of the real
exposure.

### How to test the fix

```python
res = pipeline.process_query("Which transactions are still unreconciled?")
assert res["facts"]["row_count"] == len(res["table"])
assert res["facts"]["amount_sum"] == round(res["table"]["amount"].sum(), 2)
```

### Fix

Send the model a **pre-computed facts block** (row count, sum, min/max/date
range, status distribution) computed in DuckDB or pandas over the *entire*
result, plus the 10-row sample clearly labelled as a sample. Never let a
narrative statistic depend on the truncated view.

---

<a id="b06"></a>
## B06 — Legal-suffix variants fail to resolve (`Stripe Inc` → NOT_FOUND)

**Severity:** 🟠 High &nbsp;|&nbsp; **File:** [resolver.py:85-98](resolver.py:85)

### What's wrong

`resolve_vendor` strips legal suffixes (`inc`, `llc`, `ltd`, …) when generating
**acronyms**, but not before **fuzzy scoring**. The suffix therefore drags
`fuzz.WRatio` below the `>= 70` threshold at
[resolver.py:92](resolver.py:92), and a real vendor is reported as absent.

This is a false negative on "Trap 2: entity permutations" — the exact scenario
the resolver exists to handle.

### Reproduce

```python
from resolver import resolve_vendor
from db import get_db_connection, get_all_vendor_names

con = get_db_connection()
known = get_all_vendor_names(con)

for probe in ["Stripe Payments", "Stripe", "Stripe Inc",
              "Deloitte Advisory", "Deloitte LLP",
              "Salesforce", "Netflix"]:
    print(f"{probe!r:22} -> {resolve_vendor(probe, known)}")
```

Observed:

```
'Stripe Payments'      -> ('MATCH', 'Stripe Payments', 1.0)
'Stripe'               -> ('MATCH', 'Stripe Payments', 0.95)
'Stripe Inc'           -> ('NOT_FOUND', None, 0.0)      <-- BUG
'Deloitte Advisory'    -> ('MATCH', 'Deloitte Advisory', 1.0)
'Deloitte LLP'         -> ('NOT_FOUND', None, 0.0)      <-- BUG
'Salesforce'           -> ('MATCH', 'Salesforce.com Inc.', 0.95)
'Netflix'              -> ('NOT_FOUND', None, 0.0)      <-- correct
```

Why the scores fall short:

```python
from rapidfuzz import fuzz
print(fuzz.WRatio("stripe inc", "Stripe Payments"))       # 63.5  -> below 70
print(fuzz.WRatio("deloitte llp", "Deloitte Advisory"))   # 55.2  -> below 70
```

Adding a legal suffix makes matching **worse**, which is backwards.

### How to test the fix

```python
SHOULD_MATCH = {
    "Stripe Inc": "Stripe Payments",
    "Stripe Payments LLC": "Stripe Payments",
    "Deloitte LLP": "Deloitte Advisory",
    "Salesforce Inc": "Salesforce.com Inc.",
    "AWS": "Amazon Web Services, Inc.",
}
for probe, expected in SHOULD_MATCH.items():
    status, entity, _ = resolve_vendor(probe, known)
    assert (status, entity) == ("MATCH", expected), (probe, status, entity)

# Guard against over-matching in the other direction.
for probe in ["Netflix", "Oracle", "Snowflake"]:
    assert resolve_vendor(probe, known)[0] == "NOT_FOUND", probe
```

### Fix

1. Normalise **both** sides before scoring — strip punctuation and the
   `LEGAL_SUFFIXES` set that already exists at the top of `resolver.py`.
2. Score with `max(fuzz.WRatio, fuzz.token_set_ratio)` on the normalised
   strings; `token_set_ratio` is far more robust to added/removed words.
3. Re-tune the threshold against the assertion set above — raising precision on
   `Netflix`-style absences while admitting suffix variants.

---

<a id="b07"></a>
## B07 — Category filter has no guardrail

**Severity:** 🟠 High &nbsp;|&nbsp; **File:** [query_builder.py:69-71](query_builder.py:69)

### What's wrong

Vendors get the full MATCH / AMBIGUOUS / NOT_FOUND treatment. Categories get a
raw substring match:

```python
payout_filters.append(f"LOWER(v.{category_col}) LIKE ?")
payout_params.append(f"%{category.lower()}%")
```

A typo or an invented category returns zero rows and the explainer says *"no
matching records"* — indistinguishable from "this category exists but had no
spend." The user is never told the category itself was not recognised.

### Reproduce

```python
from query_builder import build_sql
from db import get_db_connection, get_anchor_date

con = get_db_connection()
anchor = get_anchor_date(con)

for cat in ["Cloud Infrastructure", "Clod Infrastructure", "NonExistentCategory"]:
    qi = build_sql({"intent": "category_summary", "date_filter": {"type": "all"},
                    "category": cat}, None, anchor)
    df = con.execute(qi["sql"], qi["params"]).df()
    print(f"{cat!r:24} rows={len(df)}  params={qi['params']}")
```

Observed:

```
'Cloud Infrastructure'   rows=1  params=['%cloud infrastructure%']
'Clod Infrastructure'    rows=0  params=['%clod infrastructure%']    <-- typo, no warning
'NonExistentCategory'    rows=0  params=['%nonexistentcategory%']    <-- invented, no warning
```

Both failures are reported to the user identically to a legitimately empty
result.

### How to test the fix

```python
res = pipeline.process_query("Show spend for category Clod Infrastructure")
assert res["status"] in ("NOT_FOUND", "AMBIGUOUS")
assert "Cloud Infrastructure" in res["answer"]     # should suggest the real one

res2 = pipeline.process_query("Show spend for category NonExistentCategory")
assert res2["status"] == "NOT_FOUND"
```

### Fix

1. Load the distinct category list at pipeline start
   (`SELECT DISTINCT category FROM vendors`) the same way `known_vendors` is
   loaded.
2. Route the parsed category through `resolve_vendor`'s logic (generalise it to
   `resolve_entity(value, candidates)`) and reuse the existing NOT_FOUND /
   AMBIGUOUS guardrail paths in `pipeline.py`.
3. Inject the real category vocabulary into the intent-parser system prompt so
   the LLM emits values that exist.

---

<a id="b08"></a>
## B08 — No comparison intent

**Severity:** 🟠 High &nbsp;|&nbsp; **Files:** [intent_parser.py:33](intent_parser.py:33), [query_builder.py](query_builder.py)

### What's wrong

The problem statement's multi-turn requirement names this case explicitly:

> "follow-up questions, like asking **how that compares to the month before**,
> should work without the user repeating context."

The system has multi-turn *context carry* (the vendor persists across turns) but
no **comparison** capability. `intent` has four values — `spend_summary`,
`transaction_list`, `reconciliation_audit`, `category_summary` — none of which
produce a two-window delta.

### Reproduce

```python
import inspect, query_builder
print("'compar' in build_sql:", "compar" in inspect.getsource(query_builder.build_sql).lower())
# -> False
```

End-to-end (needs `GROQ_API_KEY`):

```python
from pipeline import FinanceAssistantPipeline
p = FinanceAssistantPipeline()

r1 = p.process_query("How much did we spend on AWS in May?")
hist = [{"role": "user", "content": "How much did we spend on AWS in May?"},
        {"role": "assistant", "content": r1["answer"]}]
r2 = p.process_query("How does that compare to the month before?", chat_history=hist)

print(r2["answer"])
print(r2["table"])
```

The response contains a single window's figure. There is no delta, no percentage
change, and no side-by-side table — the comparison the user asked for is absent.

### How to test the fix

```python
r2 = p.process_query("How does that compare to the month before?", chat_history=hist)
assert r2["intent"]["intent"] == "comparison"
assert set(r2["table"]["period"]) == {"2024-04", "2024-05"}
assert "delta" in r2["table"].columns and "pct_change" in r2["table"].columns
# The delta must come from SQL, not the LLM.
assert r2["table"]["delta"].iloc[0] == (
    r2["table"]["total_spend"].iloc[1] - r2["table"]["total_spend"].iloc[0])
```

### Fix

Add a `comparison` intent that carries two date windows and emits a single SQL
statement computing both periods and the delta:

```sql
SELECT period, SUM(amount) AS total_spend,
       SUM(amount) - LAG(SUM(amount)) OVER (ORDER BY period) AS delta
FROM (
  SELECT CASE WHEN payout_date BETWEEN ? AND ? THEN 'prior' ELSE 'current' END AS period,
         amount
  FROM vendor_payouts p JOIN vendors v ON p.vendor_id = v.vendor_id
  WHERE v.vendor_name = ? AND p.payout_date BETWEEN ? AND ?
) GROUP BY period ORDER BY period;
```

Keeping the subtraction in SQL preserves the zero-LLM-arithmetic guarantee.
This is roughly 60 lines and directly answers a requirement the brief names by
example.

---

<a id="b09"></a>
## B09 — LLM output is never validated

**Severity:** 🟠 High &nbsp;|&nbsp; **Files:** [pipeline.py:29-31](pipeline.py:29), [query_builder.py](query_builder.py)

### What's wrong

`parse_intent_llm` returns raw parsed JSON straight into `build_sql`. Nothing
checks that `intent`, `relative_value`, or `reconciliation_status` are members
of their declared enums. `build_sql`'s dispatch ends in a bare `else`, so an
unrecognised intent **silently becomes `transaction_list`** rather than raising.

`response_format={"type": "json_object"}` enforces JSON *syntax*, not your
*schema*.

### Reproduce

```python
from query_builder import build_sql
from db import get_db_connection, get_anchor_date

con = get_db_connection()
anchor = get_anchor_date(con)

qi = build_sql({"intent": "totally_made_up_intent",
                "date_filter": {"type": "all"}, "category": None},
               "Acme Corporation", anchor)

print("query_type reported:", qi["query_type"])
print("SQL actually built :", qi["sql"].splitlines()[1].strip())
```

Observed:

```
query_type reported: totally_made_up_intent
SQL actually built : p.payout_date AS payout_date,
```

The dict reports the fabricated intent while a completely different query runs.
Downstream code branching on `query_type` (the fallback explainer does exactly
this) then takes the wrong path.

This is the same defect your own security test walks into —
[test_suite.py:159](test_suite.py:159) passes `"category_spend"`, which is not a
real intent (see [B19](#b19)).

### How to test the fix

```python
import pytest
with pytest.raises(ValueError):
    build_sql({"intent": "totally_made_up_intent", "date_filter": {"type": "all"}},
              None, anchor)

# Valid intents still work.
for good in ["spend_summary", "transaction_list", "reconciliation_audit", "category_summary"]:
    assert build_sql({"intent": good, "date_filter": {"type": "all"}}, None, anchor)
```

### Fix

Add a single `normalize_intent(raw: dict) -> dict` at the top of
`process_query` that:

- coerces `intent` to the enum, raising or falling back to `spend_summary`
  **with a logged warning** rather than silently;
- validates `relative_value` against the canonical vocabulary (see
  [B01](#b01));
- validates `reconciliation_status` against `config.RECONCILIATION_VALUES`;
- format-checks absolute dates (see [B11](#b11)).

One function, one place, and it closes B01/B09/B11 together.

---

<a id="b10"></a>
## B10 — Test suite asserts shape, not values

**Severity:** 🟠 High &nbsp;|&nbsp; **File:** [test_suite.py:34](test_suite.py:34)

### What's wrong

The headline claim in `README.md` and `MODEL_CHOICE.md` is **"13/13 passed, 0%
math error."** But the checks are structural. Test 1:

```python
"check": lambda res: res["table"] is not None and len(res["table"]) > 0
```

This passes if the total is off by $50,000. For a criterion weighted at **30%
(Accuracy & grounding)**, the suite that substantiates the claim does not
actually verify a single number.

### Reproduce

Break the arithmetic deliberately and watch the suite stay green:

```bash
# Corrupt the aggregate: SUM -> MAX
python - <<'EOF'
import re, pathlib
p = pathlib.Path("query_builder.py")
s = p.read_text(encoding="utf-8")
p.with_suffix(".py.bak").write_text(s, encoding="utf-8")
p.write_text(s.replace("ROUND(SUM(p.", "ROUND(MAX(p.", 1), encoding="utf-8")
EOF

python test_suite.py          # Test 1 still reports PASS

# restore
mv query_builder.py.bak query_builder.py
```

Test 1 reports `[PASS]` while `total_spend` is now a maximum rather than a sum.

### How to test the fix

Add golden values, computed independently in pandas so the assertion does not
share code with the thing under test:

```python
import pandas as pd

payouts = pd.read_csv("data/vendor_payouts.csv")
vendors = pd.read_csv("data/vendor_list.csv")
j = payouts.merge(vendors, on="vendor_id")
mask = (j.vendor_name == "Acme Corporation") & \
       j.payout_date.between("2024-05-01", "2024-05-31") & \
       (j.status == "Completed")
EXPECTED = round(j.loc[mask, "amount"].sum(), 2)

res = pipeline.process_query("How much did we spend on Acme Corporation in May 2024?")
got = float(res["table"]["total_spend"].iloc[0])
assert abs(got - EXPECTED) < 0.01, f"got {got}, expected {EXPECTED}"
```

### Fix

Give every numeric test case an `expected_value`, and assert on it. Also assert
the **resolved date window** and the **resolved vendor** per case, so
[B01](#b01)-class regressions are caught by the suite rather than by a judge.

---

<a id="b11"></a>
## B11 — Absolute dates are unvalidated

**Severity:** 🟡 Medium &nbsp;|&nbsp; **Files:** [query_builder.py:52-54](query_builder.py:52), [pipeline.py:80-92](pipeline.py:80)

### What's wrong

When `date_filter.type == "absolute"`, `start_date` / `end_date` are taken from
the LLM verbatim and bound straight into SQL. There are two failure modes:

- **Malformed** (`"05/01/2024"`, `"May 2024"`) → DuckDB raises
  `ConversionException`, which `pipeline.py` catches and renders to the user as
  a raw database error string.
- **Well-formed but wrong** (wrong year, inverted range) → zero rows, reported
  as "no matching records" with no hint that the window was suspect.

### Reproduce

```python
from query_builder import build_sql
from db import get_db_connection, get_anchor_date

con = get_db_connection()
anchor = get_anchor_date(con)

def try_dates(s, e):
    qi = build_sql({"intent": "spend_summary",
                    "date_filter": {"type": "absolute", "start_date": s, "end_date": e},
                    "category": None}, "Acme Corporation", anchor)
    try:
        df = con.execute(qi["sql"], qi["params"]).df()
        print(f"{s!r:12}..{e!r:12} -> rows={len(df)}")
    except Exception as ex:
        print(f"{s!r:12}..{e!r:12} -> RAISES {type(ex).__name__}: {str(ex)[:60]}")

try_dates("2024-05-01", "2024-05-31")   # correct
try_dates("2023-05-01", "2023-05-31")   # wrong year
try_dates("2024-05-31", "2024-05-01")   # inverted
try_dates("05/01/2024", "05/31/2024")   # malformed
```

Observed:

```
'2024-05-01'..'2024-05-31' -> rows=1
'2023-05-01'..'2023-05-31' -> rows=0        <-- silent
'2024-05-31'..'2024-05-01' -> rows=0        <-- silent, inverted range
'05/01/2024'..'05/31/2024' -> RAISES ConversionException: invalid date field format
```

The last one surfaces to the end user as
*"An error occurred while executing the database query: Conversion Error…"*

### How to test the fix

```python
import pytest
for bad in [("05/01/2024", "05/31/2024"), ("May 2024", "May 2024")]:
    with pytest.raises(ValueError):
        build_sql({"intent": "spend_summary",
                   "date_filter": {"type": "absolute",
                                   "start_date": bad[0], "end_date": bad[1]}},
                  None, anchor)

# Inverted ranges must be caught, not silently empty.
with pytest.raises(ValueError):
    build_sql({"intent": "spend_summary",
               "date_filter": {"type": "absolute",
                               "start_date": "2024-05-31", "end_date": "2024-05-01"}},
              None, anchor)
```

### Fix

In `normalize_intent` (see [B09](#b09)): parse both dates with
`datetime.strptime(..., "%Y-%m-%d")`, swap or reject inverted ranges, and warn
when the window lies entirely outside `[MIN(date), MAX(date)]` of the dataset —
*"You asked about May 2023, but our records run Feb–May 2024."* That last
message is a genuinely good guardrail answer, not just an error.

---

<a id="b12"></a>
## B12 — `chart_of_accounts` never queried; incidental transactions invisible

**Severity:** 🟡 Medium &nbsp;|&nbsp; **Files:** [query_builder.py](query_builder.py), [config.py](config.py)

### What's wrong

Two provided datasets are effectively dead:

1. `chart_of_accounts` is loaded into DuckDB by `load_all_tables` and **never
   referenced by any query**. The organisers list it as a provided resource.
2. `transactions` is used **only** for `reconciliation_audit`. All spend intents
   read `vendor_payouts` exclusively — so the 60 incidental transactions with no
   linked payout are invisible to every spend question.

### Reproduce

```bash
grep -rn "chart_of_accounts\|TABLE_ACCOUNTS\|account_id" --include=*.py . \
  | grep -v config.py | grep -v data_generator.py
```

Observed — a single hit, and it is a caption in the diagram script:

```
./generate_diagram.py:187: "   5. chart_of_accounts (ledger codes)"
```

Quantifying the invisible spend:

```python
import duckdb, config, os
con = duckdb.connect(":memory:")
for t, m in config.SCHEMA_CONFIG.items():
    p = os.path.join(config.DATA_DIR, m["file"]).replace("\\", "/")
    con.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM read_csv_auto('{p}');")

print(con.execute("""
  SELECT COUNT(*) AS n, ROUND(SUM(amount), 2) AS total
  FROM transactions WHERE payout_id IS NULL
""").df())
```

Observed:

```
    n      total
0  60  149811.62
```

$149,811.62 of ledger activity that no spend query can reach.

### How to test the fix

```python
res = pipeline.process_query("Show spend by account type")
assert "chart_of_accounts" in res["sql"].lower()
assert set(res["table"]["account_type"]) >= {"Expense"}
```

### Fix

1. Add an `account_summary` intent joining
   `transactions → chart_of_accounts` and grouping by `account_name` /
   `account_type`.
2. Decide and **document** whether "spend" means payouts, ledger debits, or
   both — then say so in the UI caption. An explicit scope statement is
   defensible; a silent omission is not.
3. If real data contains `Credit` rows (refunds), filter `transaction_type`
   — the shipped data is 100% `Debit`, so this is latent rather than active.

---

<a id="b13"></a>
## B13 — Current question is duplicated into its own chat history

**Severity:** 🟡 Medium &nbsp;|&nbsp; **File:** [app.py:160-163](app.py:160)

### What's wrong

```python
st.session_state.messages.append({"role": "user", "content": user_prompt})   # line 160
...
result = pipeline.process_query(user_prompt, chat_history=st.session_state.messages)  # line 163
```

The message is appended **before** being passed as history, so
`parse_intent_llm` builds a message list where the current question appears
twice — once from `chat_history[-4:]` and again as the final user turn. This
eats one of only four history slots and can bias `is_followup` detection,
since the fallback's context scan at
[intent_parser.py:186-197](intent_parser.py:186) may match the vendor from the
*current* question and mark it as inherited.

### Reproduce

```python
import intent_parser
orig = intent_parser.parse_intent_fallback

msgs = [{"role": "user", "content": "What did we spend on CloudScale in April?"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "What about in May?"}]   # already appended

# Mirror what app.py hands to the parser:
print("history passed:", [m["content"] for m in msgs[-4:]])
print("plus current question appended again: 'What about in May?'")
```

The final list sent to Groq contains `"What about in May?"` twice.

### How to test the fix

```python
captured = {}
def spy(user_query, chat_history=[], **kw):
    captured["history"] = [m["content"] for m in chat_history]
    return orig(user_query, chat_history, **kw)

# After the fix, the current question must NOT appear in history.
assert "What about in May?" not in captured["history"]
```

### Fix

Pass `st.session_state.messages[:-1]`, or move the `append` to after the
`process_query` call.

---

<a id="b14"></a>
## B14 — Duplicate download-button key crashes the UI

**Severity:** 🟡 Medium &nbsp;|&nbsp; **File:** [app.py:136](app.py:136)

### What's wrong

```python
key=f"dl_{hash(msg['content'])}"
```

The history-render loop keys each CSV download button by the **hash of the
answer text**. Two identical answers produce the same key, and Streamlit raises
`StreamlitDuplicateElementKey`, breaking the whole page — not just that widget.
The live-render path uses `len(st.session_state.messages)`
([app.py:191](app.py:191)), which is a different scheme and can also collide
after a "Clear Conversation".

### Reproduce

1. `streamlit run app.py`
2. Click the sidebar prompt **"Show total spend by category"**.
3. Wait for the answer, then click the **same** button again.
4. On the rerun, both history entries hash identically →
   `StreamlitDuplicateElementKey: There are multiple elements with the same key='dl_...'`

Deterministic answers (empty-result messages, guardrail responses like
*"I don't have data for vendor 'Netflix'"*) collide most readily, because their
text is byte-identical every time.

### How to test the fix

```python
# Keys must be unique per message position, not per content.
msgs = [{"content": "same answer"}, {"content": "same answer"}]
keys = [f"dl_{i}" for i, _ in enumerate(msgs)]
assert len(set(keys)) == len(keys)
```

### Fix

Key by message index from `enumerate`:

```python
for i, msg in enumerate(st.session_state.messages):
    ...
    st.download_button(..., key=f"dl_hist_{i}")
```

and use a distinct prefix for the live path so the two schemes cannot collide.

---

<a id="b15"></a>
## B15 — Single DuckDB connection shared across Streamlit reruns

**Severity:** 🟡 Medium &nbsp;|&nbsp; **Files:** [pipeline.py:20](pipeline.py:20), [app.py](app.py) `@st.cache_resource`

### What's wrong

`FinanceAssistantPipeline.__init__` stores one `duckdb.connect()` handle, and
`@st.cache_resource` shares that single pipeline instance across **all** sessions
and script reruns. A DuckDB connection object is not designed for concurrent use
from multiple threads; Streamlit runs each session in its own `ScriptRunner`
thread. Two users (or one user double-clicking during a slow LLM call) can
interleave `con.execute(...)` calls on the same handle.

Symptoms are non-deterministic: a result set belonging to another query, or a
`Connection Error`. It will not show up in single-user demo testing, which is
what makes it worth writing down.

### Reproduce

```python
import threading
from pipeline import FinanceAssistantPipeline

p = FinanceAssistantPipeline()
errors = []

def hammer(q):
    for _ in range(25):
        try:
            p.process_query(q)
        except Exception as e:
            errors.append(repr(e))

ts = [threading.Thread(target=hammer, args=(q,)) for q in [
    "How much did we spend on Acme Corporation in May 2024?",
    "Which transactions are still unreconciled?",
    "Show total spend by category",
]]
for t in ts: t.start()
for t in ts: t.join()
print("errors:", len(errors))
for e in errors[:3]: print(" ", e)
```

### How to test the fix

The same script must report `errors: 0` and consistent totals across runs.

### Fix

Take a short-lived cursor per query — DuckDB cursors are cheap and isolated:

```python
def _q(self, sql, params):
    cur = self.con.cursor()
    try:
        return cur.execute(sql, params).df()
    finally:
        cur.close()
```

Route `pipeline.py` and `anomaly.py` through it.

---

<a id="b16"></a>
## B16 — Ships the largest permitted model with no smaller-model benchmark

**Severity:** 🟡 Medium (but worth **20%** of the score) &nbsp;|&nbsp; **Files:** [config.py](config.py), [MODEL_CHOICE.md](MODEL_CHOICE.md)

### What's wrong

Section 7 of the problem statement:

> "Build this using the smallest model that can still deliver accurate answers.
> Bigger is not better here… **Defaulting to the largest available frontier
> model, without justification, will be scored down.**"

`ACTIVE_MODEL = "openai/gpt-oss-20b"` is the **largest model the rules allow**,
and the stated justification is that it fits under the ceiling. That is
compliance, not the optimisation being scored.

Two specific risks:

1. `gpt-oss-20b` is ~21B *total* parameters (≈3.6B active, mixture-of-experts).
   A judge reading the parameter count literally could score it as exceeding the
   20B cap. `MODEL_CHOICE.md` asserts "**20B parameters** (strictly satisfies the
   ≤ 20B ceiling)" without addressing this.
2. The LLM only does JSON intent extraction and result summarisation. Neither
   task plausibly needs 20B.

### Reproduce

```bash
grep -n "ACTIVE_MODEL" config.py
grep -rn "8b\|9b\|smaller model\|benchmark.*model" MODEL_CHOICE.md
```

The first returns the 20B default; the second returns no comparison against any
smaller model.

### How to test the fix

Create `benchmark_models.py` that runs the full eval set against each candidate
and emits a table:

```python
MODELS = ["llama-3.1-8b-instant", "gemma2-9b-it", "openai/gpt-oss-20b"]
for m in MODELS:
    os.environ["GROQ_MODEL"] = m
    acc, p50_ms, cost = run_eval_set()
    print(f"{m:26} accuracy={acc:5.1%} p50={p50_ms:6.1f}ms cost=${cost:.5f}/query")
```

Ship the **smallest** model that scores 100%, and paste the table into
`MODEL_CHOICE.md` and the deck.

### Fix

Run the benchmark and let it decide. "We tested 8B, 9B and 20B; 8B matched 20B
on all 13 cases at 3× the speed and a fifth of the cost, so we shipped 8B" is a
far stronger answer than "we stayed under the cap" — and it is the single
highest-leverage change available, since model efficiency carries 20% of the
total score.

---

<a id="b17"></a>
## B17 — Stale `gpt-oss-120b` references

**Severity:** 🟢 Low &nbsp;|&nbsp; **Files:** [intent_parser.py:5](intent_parser.py:5), [intent_parser.py:57](intent_parser.py:57), [explainer.py:5](explainer.py:5), [explainer.py:27](explainer.py:27), [build_deck.py:10](build_deck.py:10)

### What's wrong

Five docstrings and comments still name `openai/gpt-oss-120b`. The code reads
`config.ACTIVE_MODEL` (`gpt-oss-20b`), so behaviour is correct — but a judge
grepping the repo for the model name finds a **120B** model referenced in the
very modules that make the LLM calls, directly contradicting the Section 7
compliance claim. `build_deck.py:10` puts it in the deck generator's own
description of the model-choice slide.

### Reproduce

```bash
grep -rn "120b" --include=*.py --include=*.md .
```

Observed:

```
build_deck.py:10:3. Model Choice Rationale (20% Scored Rubric: Groq LPU + gpt-oss-120b)
explainer.py:5:Uses Groq with 'openai/gpt-oss-120b'.
explainer.py:27:    # Call Groq with openai/gpt-oss-120b
intent_parser.py:5:Uses Groq with 'openai/gpt-oss-120b' for ultra-fast inference...
intent_parser.py:57:    """Uses Groq with openai/gpt-oss-120b to extract structured intent."""
```

### How to test the fix

```bash
grep -rn "120b" --include=*.py --include=*.md . && echo "STALE REFS FOUND" || echo "clean"
```

### Fix

Replace all five with a reference to `config.ACTIVE_MODEL` rather than a
hardcoded name, so this cannot drift again.

---

<a id="b18"></a>
## B18 — Fallback parser never populates `category`

**Severity:** 🟢 Low &nbsp;|&nbsp; **File:** [intent_parser.py:200](intent_parser.py:200)

### What's wrong

`parse_intent_fallback` hardcodes `"category": None` in its return dict. The
category filter therefore only ever works when the Groq call succeeds. Without
an API key — or during a rate limit, which is exactly when the fallback
matters — every category-scoped question silently degrades to an unfiltered
query.

This also means **test 10** ("Show spend for category NonExistentCategory",
expecting 0 rows) passes for the wrong reason on the fallback path: it returns
0 rows only when the LLM supplies the category; with the fallback it returns a
full unfiltered category summary.

### Reproduce

```python
import os
os.environ.pop("GROQ_API_KEY", None)   # force the fallback path

from intent_parser import parse_intent_fallback
from datetime import date

out = parse_intent_fallback("Show spend for category Cloud Infrastructure",
                            anchor_date=date(2024, 5, 31))
print(out["intent"], "| category =", out["category"])
```

Observed:

```
category_summary | category = None
```

The category named in the question is dropped.

### How to test the fix

```python
out = parse_intent_fallback("Show spend for category Cloud Infrastructure",
                            anchor_date=date(2024, 5, 31),
                            known_categories=["Cloud Infrastructure", "Audit & Legal"])
assert out["category"] == "Cloud Infrastructure"
```

### Fix

Pass the distinct category list into the fallback (mirroring `known_vendors`)
and match against it with the same containment-then-fuzzy approach used for
vendors. Combine with [B07](#b07) so both paths resolve categories identically.

---

<a id="b19"></a>
## B19 — Injection test uses a non-existent intent name

**Severity:** 🟢 Low &nbsp;|&nbsp; **File:** [test_suite.py:159](test_suite.py:159)

### What's wrong

```python
injection_intent = {"intent": "category_spend", "category": "Cloud' OR '1'='1"}
```

`category_spend` is not one of the four real intents. Because of
[B09](#b09), it falls through `build_sql`'s dispatch to the default
`transaction_list` branch. The parameterisation **is** genuinely proven — but on
a different code path from the one the test name implies, and the
`category_summary` branch that a category-injection would actually target is
never exercised.

### Reproduce

```python
from query_builder import build_sql
from db import get_db_connection, get_anchor_date

con = get_db_connection(); anchor = get_anchor_date(con)
qi = build_sql({"intent": "category_spend", "category": "Cloud' OR '1'='1"}, None, anchor)
print("branch taken:", "category_summary" if "GROUP BY" in qi["sql"] else "transaction_list")
```

Observed: `branch taken: transaction_list`

### How to test the fix

```python
for intent_name in ["spend_summary", "transaction_list", "category_summary"]:
    qi = build_sql({"intent": intent_name, "date_filter": {"type": "all"},
                    "category": "Cloud' OR '1'='1"}, None, anchor)
    assert "?" in qi["sql"]
    assert any("or '1'='1" in str(p).lower() for p in qi["params"])
    assert len(con.execute(qi["sql"], qi["params"]).df()) == 0
```

### Fix

Use `category_summary`, and loop the payload across **every** intent so the
guarantee is proven on all branches. Add a vendor-name injection payload too —
`resolved_vendor` is likewise bound, and that should be asserted rather than
assumed.

---

<a id="b20"></a>
## B20 — 20M-record and `<5ms` claims are unmeasured

**Severity:** 🟢 Low &nbsp;|&nbsp; **Files:** [README.md](README.md), [MODEL_CHOICE.md](MODEL_CHOICE.md)

### What's wrong

Both documents state DuckDB handles the Section 7 limit of 20M records "in
sub-5ms latency", and quote `~$0.0002/query` and `500+ tok/s`. The shipped
dataset is **71 payouts and 131 transactions**. Nothing in the repository
measures any of these figures — the throughput and cost numbers come from Groq's
published specs, not from instrumentation here.

The claims are plausible. They are just not evidenced, and a judge who asks
"what did you measure?" should get a number.

### Reproduce

```bash
wc -l data/*.csv
grep -rn "20M\|sub-5ms\|500+ tok" README.md MODEL_CHOICE.md | head
ls benchmark* scale* 2>/dev/null || echo "no benchmark script in repo"
```

### How to test the fix

Add `benchmark_scale.py`:

```python
import duckdb, time

con = duckdb.connect(":memory:")
con.execute("""
CREATE TABLE vendor_payouts AS
SELECT 'PAY-' || i AS payout_id,
       DATE '2024-01-01' + (i % 150) AS payout_date,
       'V' || (i % 500) AS vendor_id,
       (random() * 20000)::DECIMAL(12,2) AS amount,
       'USD' AS currency, 'Completed' AS status, '' AS description
FROM range(1, 20_000_001) t(i);
""")

t0 = time.perf_counter()
con.execute("""SELECT vendor_id, SUM(amount) FROM vendor_payouts
               WHERE payout_date BETWEEN '2024-04-01' AND '2024-04-30'
               GROUP BY vendor_id""").fetchall()
print(f"20M-row grouped aggregate: {(time.perf_counter() - t0) * 1000:.1f} ms")
```

### Fix

Run it, put the real number in `README.md` and the deck, and replace the vendor
spec sheet figures with measured p50/p95 latency from your own eval run.

---

## Cross-cutting recommendation: numeric verification of explainer output

Not a bug in the current code — a missing safeguard that closes the last gap in
the grounding story, and the natural companion to [B03](#b03) and [B05](#b05).

Today, nothing checks that the numbers in the LLM's prose actually came from the
database. The prompt *asks* the model not to invent figures, but the output is
never verified.

```python
import re

def verify_grounding(answer: str, df, extra_facts: set[float]) -> bool:
    """Every currency figure in the prose must appear in the computed facts."""
    allowed = set(extra_facts)
    for col in df.select_dtypes("number").columns:
        allowed.update(round(float(v), 2) for v in df[col].dropna())
    for token in re.findall(r"\d[\d,]*\.\d{2}", answer):
        if round(float(token.replace(",", "")), 2) not in allowed:
            return False
    return True
```

Wire it into `generate_explanation`: if verification fails, discard the LLM text
and render the existing deterministic template instead. Roughly 25 lines, and it
upgrades the pitch from *"we ask the model not to hallucinate"* to **"our system
cannot emit a number the database did not produce"** — a claim you can
demonstrate live.

---

## Suggested fix order

| Order | Items | Rationale |
|-------|-------|-----------|
| 1 | [B01](#b01), [B02](#b02), [B03](#b03) | Wrong numbers presented with high confidence; all on likely demo questions |
| 2 | [B09](#b09), [B11](#b11) | One `normalize_intent()` closes B01/B09/B11 together |
| 3 | [B06](#b06), [B07](#b07) | Guardrail correctness — 15% NLU criterion |
| 4 | [B08](#b08) | Requirement named by example in the brief |
| 5 | [B16](#b16) | 20% of total score, and only needs a benchmark run |
| 6 | [B10](#b10), [B19](#b19) | Make the "13/13, 0% error" claim actually load-bearing |
| 7 | [B04](#b04), [B05](#b05), numeric verification | Grounding hardening |
| 8 | [B12](#b12)–[B15](#b15), [B17](#b17), [B18](#b18), [B20](#b20) | Coverage, robustness, polish |
