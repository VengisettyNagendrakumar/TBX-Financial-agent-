# Architecture V2 — Migration Plan

Rebuild plan for the new 3-table bank schema, agentic tool-calling, clarifying
questions, encryption in transit, and 4M+ row latency targets.

All latency figures in this document were **measured** on a 4M-row synthetic
table built to the new schema, not estimated. See [§5](#5-latency-plan).

---

## 1. What changed

### Schema

| Old (5 tables) | New (3 tables) |
|---|---|
| `vendors` (canonical vendor list) | **gone** — no vendor table exists |
| `vendor_payouts` | `transaction` |
| `transactions` | `transaction` |
| `reconciliation_status` | **gone** |
| `chart_of_accounts` | **gone** |
| — | `bank`, `account` |

### Requirements

| Dropped | Added |
|---|---|
| Reconciliation audits | Merchant spend: *"how much on Swiggy last month"* |
| Non-binary status handling | All-time totals: *"how much on Zomato total"* |
| Vendor/payout separation | Ranking: *"which vendor have I spent on the most"* |
| | Inbound credits from people: *"how much did my friend pay me in the last 3 months"* |
| | Agent with a tool array, not a linear pipeline |
| | Clarifying follow-up questions |
| | Encryption in transit |
| | 4M+ rows, latency-optimised |

---

## 2. The central problem: there is no vendor table

This single fact drives the whole redesign.

Today, `resolver.py` fuzzy-matches user input against `SELECT vendor_name FROM
vendors` — a clean canonical list. **That list no longer exists.** "Swiggy" now
appears only inside free-text `description` narration, in bank-rail-specific
formats:

```
UPI-NAVYUG SELECTION-XXXXXX8672-AUBL0002125-103293775381-260514201735136
NEFT  - UTIB0002678 - 95604250 - 915020031685136 - UMANG SELECTIONHAPURBPES
IMPS/P2A/600228462725/UTIB/918020101986700/00/INET/9211/SELECTIONMALIGAI/...
IMPS OW/507614422198/Gautam singh/SBIN/43292707719
FT -  95842568 -  50200013729069 - SELECTION ELECTRONICS   DAHISAR EAST
R/RATNR52025121600100235/ZBFLCTP405PBL15667333//SELECTRICITY TWO PRIVATE LIMITED/...
NEFT/000483399203/ICIC/PARESH VIKRANT GHASE
```

Two consequences:

1. **"Which vendor did I spend the most on?" is impossible without a grouping
   key.** You cannot `GROUP BY` a substring you haven't extracted. Runtime regex
   over 4M descriptions is both slow (43ms/query, and wrong — it grabs arbitrary
   uppercase runs, not merchants).
2. **`LIKE '%swiggy%'` cannot be indexed.** A leading wildcard forces a full scan
   of every description on every query.

**The fix — and the core of V2: derive the missing dimension at ingest.** Parse
each narration once, extract and normalise the counterparty, store it as an
indexed column, and pre-aggregate. This recreates the `vendors` table the schema
no longer gives us, and everything else (resolution, ranking, guardrails,
anomalies) is built on it exactly as before.

---

## 3. Target architecture

```
                              User (HTTPS / TLS 1.3)
                                       │
                        ┌──────────────▼───────────────┐
                        │  FastAPI backend + chat UI   │
                        │  session → entity_id (server │
                        │  side, never model-supplied) │
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────▼───────────────┐
                        │      AGENT (tool loop)       │
                        │  ≤20B model, function calls  │
                        │  max 6 iterations            │
                        └──────────────┬───────────────┘
                                       │ typed tool args (never SQL)
   ┌──────────┬──────────┬─────────────┼───────────┬────────────┬──────────┐
   ▼          ▼          ▼             ▼           ▼            ▼          ▼
resolve_   resolve_   query_       top_        list_        get_       ask_user
merchant   time_range  spend      counter-    transactions  balance   (clarify)
                                  parties
   │          │          │             │           │            │          │
   └──────────┴──────────┴──────┬──────┴───────────┴────────────┘          │
                                │  parameterised SQL only                   │
                   ┌────────────▼─────────────┐                            │
                   │   DuckDB analytical      │                            │
                   │   ├── rollup_monthly     │ ◄── 99.9% of queries       │
                   │   ├── txn_fact (enriched)│ ◄── drill-down             │
                   │   └── merchant_dim       │ ◄── resolver source        │
                   └────────────▲─────────────┘                            │
                                │ one-time ETL + enrichment (~13s / 4M)    │
                   ┌────────────┴─────────────┐                            │
                   │  MySQL (system of record)│                            │
                   │  TLS, ssl_verify_identity│                            │
                   └──────────────────────────┘                            │
                                                                            │
                        clarification returns to user ◄──────────────────────┘
```

**Invariant carried over from V1:** the model chooses *tools and arguments*; it
never writes SQL and never does arithmetic. Every number still comes from
DuckDB.

**New invariant:** `entity_id` is injected server-side from the session. The
model can choose *what* to filter, never *whose data* to query.

---

## 4. Data layer

### 4.1 Ingest → enrich → roll up

Three tables built once at startup (or on a refresh trigger):

**`txn_fact`** — every transaction, plus derived columns:

| Derived column | Purpose |
|---|---|
| `entity_id` | denormalised from `account` — enables single-table scoping |
| `merchant_norm` | canonical counterparty key (the recreated vendor dimension) |
| `merchant_display` | human-readable form for answers |
| `counterparty_kind` | `merchant` \| `person` \| `bank_charge` \| `self_transfer` \| `unknown` |
| `channel` | `UPI` \| `NEFT` \| `IMPS` \| `FT` \| `RTGS` \| `CARD` \| `OTHER` |
| `txn_month` | `date_trunc('month', transaction_date)` for pruning |
| `utr_blind_idx` | HMAC of UTR for searchability — see [§6.3](#63-the-utr-problem) |

Physically sorted by `(entity_id, merchant_norm, transaction_date)` so DuckDB's
zone maps prune aggressively.

**`rollup_monthly`** — `(entity_id, merchant_norm, txn_month, transaction_type)`
→ `SUM`, `COUNT`, `MIN`, `MAX`. Measured: **4,000,000 rows → 368,136 rows**,
built in 0.5s. This answers nearly every question asked. It is written sorted by
`(entity_id, merchant_norm, txn_month)`; that physical order is what the zone
maps prune on, and it is worth ~35% of the query latency.

**`merchant_dim`** — distinct merchants per entity with transaction counts, date
span, and kind. This is the resolver's candidate list — the direct replacement
for `SELECT vendor_name FROM vendors`.

### 4.2 Merchant extraction

Extraction is **generic, not per-rail**. Fixed field positions drift between
banks — in the provided sample the IMPS/P2A name sits at field 9 while IMPS OW
puts it at field 3 — so hardcoding positions is brittle. Instead: split on the
dominant delimiter (`/`, then ` - `, then `-`), then keep the *most name-like*
field.

A field survives when it is purely alphabetic (3+ chars), which excludes
reference codes like `ZBFLCTP5L2PBL2933` and `INWD48` because they contain
digits; is not a rail keyword (`UPI`, `NEFT`, `INET`, `P2A`, …); and is not a
masked-account run of X's. The longest survivor wins, and any trailing location
after a 2+ space run is dropped (`SELECTION ELECTRONICS   DAHISAR EAST`).

| Narration | Extracted |
|---|---|
| `UPI-TATA POWER-XXXXXX9300-HDFC0009348-...` | `TATA POWER` |
| `IMPS/P2A/6000.../HDFC/9180.../00/INET/2960/NAVYUG SELECTION/ZBFL.../INWD48` | `NAVYUG SELECTION` |
| `IMPS OW/5076.../Divya Reddy/SBIN/4329...` | `Divya Reddy` |
| `NEFT  - ICIC0005218 - 95595159 - 9150... - LENSKART LIMITED` | `LENSKART LIMITED` |
| `FT -  95380307 -  50200014109376 - FLIPKART   VIMAN NAGAR` | `FLIPKART` |
| `R/SBINR5588058/ZBFLCTP405PBL88063//BIGBASKET LIMITED/REF8116` | `BIGBASKET LIMITED` |
| `TRF/27964914/15335598` | *(none → UNKNOWN)* |

**IFSC bank codes must be stopped explicitly.** They are four alphabetic
characters, so they pass the filter and beat any shorter brand on the
longest-field rule. Measured before the fix: 46,348 of 4M rows (1.16%) resolved
to `HDFC` or `SBIN` instead of `OLA`, `UBER` or `JIO`. The codes are read from
the `bank` table at ingest and added to the stopword list, which takes the
leakage to zero.

Then normalise: uppercase → strip legal suffixes (`PRIVATE LIMITED`, `LTD`,
`LLP`, `PVT`) → strip trailing location tokens (`DAHISAR EAST`, `SAKET DELHI`) →
collapse whitespace → alias-map brand↔legal names (`BUNDL TECHNOLOGIES` →
`SWIGGY`).

**Coverage is a first-class metric.** Anything unparsed becomes
`merchant_norm = 'UNKNOWN'` and is *reported as unattributed*, never silently
dropped. Target ≥90% on the real export; publish the number. This carries V1's
grounding discipline into the new dimension: an answer that says "₹4.2L across
identified merchants, ₹31k unattributed" is honest; one that quietly omits 8% is
not.

> Step-by-step ingest and query traces, including the late-posting lookback, the
> alias-map versioning rule, and the rollup-vs-fact routing rule, are in
> [§12 Data flows in detail](#12-data-flows-in-detail).

### 4.3 Person vs merchant

Needed for *"how much did my friend pay me"*. A counterparty is classified
`person` when it has no corporate suffix, is 2–3 title-case-ish tokens, arrives
on a P2P/P2A rail, and has low transaction frequency. `bank_charge` (e.g. "IMPS
charges") and `self_transfer` are excluded from "which vendor did I spend most
on" — otherwise bank fees and own-account moves pollute the ranking.

---

## 5. Latency plan

### Measured, 4M rows, scoped to one entity

Figures below are end-to-end through the Phase 2 query layer (which runs a
facts query plus a rows query per call), on a realistic dataset: 200 entities,
2,000 accounts, 65 counterparties each, **368,136 rollup rows**.

| Operation | Naive (parse at query time) | Via query layer | Source |
|---|---|---|---|
| Spend on one merchant, one month | ~36 ms | **6.8 ms** | rollup |
| Spend on one merchant, all time | ~36 ms | **7.0 ms** | rollup |
| Top counterparties | ~43 ms | **14.3 ms** | rollup |
| Credits from people, 3 months | — | **9.6 ms** | rollup |
| Compare two periods | — | **14.4 ms** | rollup ×2 |
| Non-month-aligned window | — | **18.2 ms** | fact fallback |
| Drill-down, 50 rows | — | **20.6 ms** | fact |
| Account balances | — | **1.8 ms** | account |

One-time build at 4M rows: land 1.9s, extract 6.0s, normalise 2.2s,
map+sort 6.1s, rollup 0.5s — **~13s total**, then reloaded instantly from the
persisted `.duckdb` file.

> An earlier draft of this section quoted 0.5–0.8 ms. Those were measured
> against a 12k-row rollup produced by a generator bug that correlated account
> choice with merchant choice, leaving each entity with only ~7 counterparties.
> With a realistic 368k-row rollup the true figures are the ones above.
>
> Two things did **not** help and are worth recording: ART indexes on
> `entity_id` (DuckDB prunes range scans with zone maps, not B-trees), and
> DataFrame conversion (~0.5 ms of a 7 ms call). Physically sorting the rollup
> by `(entity_id, merchant_norm, txn_month)` did help — that is what the zone
> maps prune on.

### Why this matters more than it looks

An agent makes **5–8 tool calls per turn**. At naive speeds that is
~250–350ms of pure database time per user turn *before* any LLM latency.
Through the query layer it is **~50–100ms**. The rest of the budget goes to the
model, where it belongs.

### Budget per turn

| Stage | Target |
|---|---|
| Tool calls (5–8 × 7–20ms) | 50–100 ms |
| LLM: intent + tool selection (≤20B on Groq) | ~300 ms |
| LLM: final summarisation | ~300 ms |
| Transport + render | ~100 ms |
| **Total** | **< 1.0 s** |

### Additional measures

- Persist the enriched DuckDB to a `.duckdb` file so restarts skip ETL.
- Cache `merchant_dim` per entity in memory for the resolver.
- Run independent tool calls concurrently (see [§8](#8-carry-over-from-bugsmd) —
  needs the connection fix).
- Cap `list_transactions` at 200 rows with explicit truncation disclosure.

---

## 6. Security

### 6.1 Encryption in transit

| Hop | Requirement |
|---|---|
| Browser → backend | TLS 1.3, HSTS, secure + `SameSite=Strict` cookies |
| Backend → MySQL | `ssl_ca` + `ssl_verify_identity=True` (reject unverified certs) |
| Backend → LLM API | HTTPS (Groq already), plus egress redaction — [§6.2](#62-pii-never-reaches-the-model) |
| Service → service | mTLS if the UI and API are split |

Streamlit has no native TLS; either terminate at Caddy/nginx, or — recommended —
move to **FastAPI + a separate chat UI**, which the brief's "chat interface plus
backend" wording already implies and which the agent's streaming needs anyway.

### 6.2 PII never reaches the model

Descriptions embed account numbers (`50200013729069`) and personal names. Before
any string leaves the process for the LLM API:

- mask account numbers to last 4 (`XXXXXX9069`)
- redact digit runs ≥ 9
- never send `utr_number` or raw `account_number` at all
- send `merchant_display`, not raw narration, wherever possible

This is both a compliance property and a good slide.

### 6.3 The UTR problem

The schema warns an encrypted column can't be searched with `WHERE =`, and the
sample `utr_number` values are **already ciphertext** (`jhI5nAdyb1qOEjmcB3Jv…`).

Decision, per the schema's own guidance:
- A bare *"reference number"* question hits **`transaction_reference_id`**
  (plaintext, indexed, directly searchable).
- **`utr_number`** is used only when the user explicitly says "UTR".
- For UTR equality search without decrypting every row, store a **blind index**:
  `utr_blind_idx = HMAC-SHA256(pepper, normalised_plaintext_utr)`, computed at
  ingest, indexed. Equality lookups work; prefix/fuzzy do not — an acceptable
  trade, and worth stating explicitly rather than pretending otherwise.
- ⚠️ **Open question** — this requires the decryption key. If the test harness
  supplies ciphertext with no key, UTR search is impossible and must return an
  honest guardrail ("UTR lookup unavailable"), not a wrong answer. Confirm with
  organisers. See [§10](#10-open-questions).

### 6.4 Tenancy

`entity_id` is resolved from the session server-side and injected into every
tool call. It is **not** a model-controllable parameter. Any tool invocation
arriving without it is rejected. Production auth is out of scope per the brief,
but data scoping is required for *correctness*, not just security — "how much
did I spend" is meaningless unscoped.

---

## 7. The agent

### 7.1 Tools

Each takes **typed, validated arguments** and returns structured JSON. None
accepts SQL.

| Tool | Args | Returns |
|---|---|---|
| `resolve_merchant` | `name` | `MATCH` / `AMBIGUOUS` / `NOT_FOUND` + candidates + confidence |
| `resolve_time_range` | `phrase` | concrete `start`/`end`, or `UNRESOLVED` |
| `query_spend` | `merchant?`, `start?`, `end?`, `direction` | total, count, avg, date span, **grand total + truncation flag** |
| `top_counterparties` | `direction`, `start?`, `end?`, `limit`, `kind?` | ranked list + grand total |
| `list_transactions` | filters, `limit≤200` | rows + total row count |
| `get_balance` | — | per-account balances (masked numbers) |
| `search_reference` | `ref`, `kind='ref'\|'utr'` | matching transactions |
| `ask_user` | `question`, `options[]` | *(suspends the turn)* |

### 7.2 Clarification as a tool, not a prompt hope

`ask_user` is the mechanism for the required follow-up behaviour. Making it a
**tool** rather than an instruction means the decision to clarify is explicit,
loggable, and testable — and can be *forced by policy* rather than left to the
model's judgement.

Policy gates that mandate a clarification:

1. No time range given **and** the merchant's history spans > 1 month.
2. `resolve_merchant` returns `AMBIGUOUS`.
3. A person reference with no name ("my friend", "my landlord").
4. Direction is genuinely unclear ("how much with Zomato" — spent, or refunded?).

Worked example — the flow from the brief:

```
User:  I want to calculate my spending for swiggy
  → resolve_merchant("swiggy") → MATCH "SWIGGY", 14 months of history
  → policy gate 1 fires
Agent: For which period? [Last month] [Last 3 months] [This year] [All time]
User:  last 3 months
  → resolve_time_range("last 3 months") → 2026-04-01 .. 2026-06-30
  → query_spend(merchant="SWIGGY", start=…, end=…, direction="debit")
Agent: ₹18,432.00 across 47 orders (Apr–Jun 2026), average ₹392.
       [table] [CSV] [audit trace]
```

And the person case:

```
User:  How much did my friend pay me in the last 3 months?
  → resolve_time_range("last 3 months") → OK
  → top_counterparties(direction="credit", kind="person", start=…, end=…)
  → policy gate 3 fires (no name given)
Agent: Which one? I see credits from [Gautam Singh ₹12,400]
       [Paresh Vikrant Ghase ₹9,241] [Someone else]
```

### 7.3 Loop control

Max 6 iterations, then force a final answer. Every tool call is recorded with
its arguments, SQL, params, row count and latency, and rendered in the existing
audit drawer — the explainability requirement now covers the agent's *reasoning
path*, which is strictly better than V1's single SQL statement.

### 7.4 Model

Section 7's ≤20B ceiling still applies (assumed — see
[§10](#10-open-questions)), with a new hard requirement: **reliable function
calling**. Candidates: `llama-3.1-8b-instant`, `gemma2-9b-it`,
`openai/gpt-oss-20b`. Benchmark all three on tool-selection accuracy and ship the
smallest that passes — this is [B16](BUGS.md) and it is still worth 20% of the
score. Keep a deterministic rule-based fallback for when tool-calling fails.

---

## 8. Carry-over from BUGS.md

| Bug | Status in V2 |
|---|---|
| [B01](BUGS.md) silent date widening | **Becomes critical.** "Last 3 months" is now a headline requirement and `last_n_months` doesn't exist. Build `resolve_time_range` with an explicit `UNRESOLVED` return that triggers clarification instead of widening. |
| [B02](BUGS.md) status not filtered | **Reappears as direction.** `transaction_type` credit/debit must always be explicit — summing credits into "spend" is the same class of error. |
| [B03](BUGS.md)/[B04](BUGS.md) totals + truncation | Same fix, still needed. `query_spend` and `top_counterparties` return a grand total and a truncation flag by contract. |
| [B05](BUGS.md) `head(10)` | Tools return a facts block; the LLM never sees only a sample. |
| [B06](BUGS.md) suffix-stripping | Folded into merchant normalisation (§4.2). |
| [B07](BUGS.md) no category guardrail | Generalised: `resolve_merchant` returns the tri-state for every entity type. |
| [B09](BUGS.md) unvalidated LLM output | Solved structurally — typed tool schemas (Pydantic) validate every argument at the boundary. |
| [B10](BUGS.md) shape-only tests | Rebuild the eval set with golden values at 4M scale. |
| [B15](BUGS.md) shared DuckDB connection | **Escalates to blocking** — concurrent tool calls need per-call cursors. |
| [B13](BUGS.md)/[B14](BUGS.md) Streamlit bugs | Moot if we move to FastAPI; otherwise still apply. |
| [B12](BUGS.md), [B17](BUGS.md)–[B20](BUGS.md) | Obsolete (old schema) or absorbed. |

---

## 9. Delivery phases

Each phase ends green and demoable.

| Phase | Work | Done when |
|---|---|---|
| **0. Foundation** | New `config.py` schema map; MySQL→DuckDB loader with TLS; drop reconciliation/vendor/payout code; 4M-row generator with realistic narration | `python -c "import db"` loads 4M rows; old modules deleted |
| **1. Enrichment** | Rail parsers, normaliser, person/merchant classifier, `merchant_dim` | Coverage ≥90% measured and printed |
| **2. Rollups + queries** | `rollup_monthly`, `txn_fact` sorted, parameterised query builders with totals + truncation | Benchmarks reproduce §5 numbers |
| **3. Resolver** | Repoint `resolver.py` at `merchant_dim`; keep MATCH/AMBIGUOUS/NOT_FOUND; add suffix fix | Tri-state assertions pass on real merchant names |
| **4. Agent** | Tool schemas, loop, `ask_user`, policy gates, audit trace | The two worked flows in §7.2 run end to end |
| **5. Security** | TLS, PII redaction, masking, blind index, entity scoping | Redaction unit tests; no PII in captured LLM payloads |
| **6. Eval** | Golden-value suite at 4M scale; model benchmark across 3 sizes | Accuracy table published; smallest passing model shipped |

Recommended order if time is short: **0 → 1 → 2 → 4 → 5**, with 3 and 6 folded
in. Phases 1–2 are the moat; phase 4 is the demo.

---

## 10. Open questions

Working assumptions, stated so the plan isn't blocked. Confirm where possible:

1. **Delivery format** — MySQL dump, or CSVs? Assumed MySQL as system of record,
   DuckDB as analytical replica. If CSVs, drop the MySQL hop; nothing else
   changes.
2. **UTR decryption key** — provided or not? Assumed **not**; UTR search degrades
   to an honest "unavailable" guardrail. See [§6.3](#63-the-utr-problem).
3. **Section 7 constraints** — assumed still in force (≤20B model, ≤20M records).
   The 4M figure you quoted sits comfortably inside the 20M limit.
4. **Entity selection** — assumed a sidebar entity picker for the demo, since
   auth is out of scope. Note the sample data reuses some UUIDs as both
   `account_id` and `entity_id`, so don't assume they're disjoint.
5. **Currency** — sample amounts look like INR; V1 formats as `$`. Confirm and
   fix the formatter.
6. **"My friend"** — is there any relationship data, or is it purely inferred
   from transaction history? Assumed inferred, hence the clarification flow.

---

## 11. What gets deleted

`anomaly.py` reconciliation baselines, `intent_parser.py`'s reconciliation
branches, all `RECONCILIATION_VALUES` config, the `vendors`/`vendor_payouts`/
`chart_of_accounts`/`reconciliation_status` schema entries, and the
reconciliation test cases. Roughly 40% of the current codebase.

**What survives, and should:** the parameterised-SQL discipline, the tri-state
resolver contract, confidence signalling, the audit trail, CSV export,
`escape_markdown_currency`, and the core principle that the model never computes
a number. Those were the right calls in V1 and they transfer intact.

---

## 12. Data flows in detail

### 12.1 Three stores, three jobs

| Store | Holds | Answers | Size at 4M |
|---|---|---|---|
| `merchant_dim` | the **vocabulary** — which counterparty names exist | "did you mean Swiggy?" | ~11,000 rows (65/entity) |
| `rollup_monthly` | the **answers** — pre-aggregated totals | "how much on Swiggy in May" | ~368,000 rows |
| `txn_fact` | the **evidence** — individual enriched rows | "show me those 47 orders" | 4,000,000 rows |

The resolver reads the vocabulary, most queries hit the answers, drill-down and
verification hit the evidence.

### 12.2 Ingest — first run

**Stage 1, land.** DuckDB attaches MySQL over TLS and streams tables in; nothing
passes through Python memory:

```sql
ATTACH 'host=… user=… ssl_mode=REQUIRED' AS src (TYPE mysql);
CREATE TABLE raw_transaction AS SELECT * FROM src.transaction;
```

**Stage 2, extract (SQL, vectorised).** The rail parsers are SQL `CASE`
expressions, **not** a Python loop — 4M rows run vectorised in ~2.4s where a row
loop would take minutes. Tracing a real row from the sample data:

```
description: 'FT -  95842568 -  50200013729069 - SELECTION ELECTRONICS   DAHISAR EAST'

  channel        ← prefix 'FT '                  → FT
  split ' - '    → [FT, 95842568, 50200013729069, SELECTION ELECTRONICS   DAHISAR EAST]
  counterparty   ← most name-like field          → 'SELECTION ELECTRONICS   DAHISAR EAST'
  strip location ← text before 2+ spaces         → 'SELECTION ELECTRONICS'
  merchant_raw                                   → 'SELECTION ELECTRONICS'
  txn_month      ← date_trunc('month', …)        → 2026-06-01
  entity_id      ← join account on account_id    → f2f5e332-c2d1-4555-9a6b-65c7cd195077
```

**Stage 3, normalise the vocabulary — not the rows.** The most important
decision in the ingest, and measured:

```
transactions             : 4,000,000
DISTINCT merchant strings:     3,000     (1 distinct per 1,333 rows)
```

Merchant strings repeat massively, so the fuzzy clustering that unifies
`SWIGGY` / `SWIGGY LTD` / `BUNDL TECHNOLOGIES` runs over **3,000 distinct
strings, not 4M rows**. Clustering 3,000 items is trivial; clustering 4M is
computationally impossible. Output is a small `merchant_alias` table
(`raw_string → canonical`).

**Stage 4, map back, sort, aggregate.** A join maps every row to its canonical.
`txn_fact` is written sorted by `(entity_id, merchant_norm, transaction_date)`
so DuckDB's zone maps skip whole row groups; `rollup_monthly` aggregates off it.

**Stage 5, persist.** Write a `.duckdb` file so restarts skip all of the above,
plus a manifest: max `transaction_date`, row count, schema hash, and
**alias-map version**.

Total: **~3s for 4M rows, paid once.**

### 12.3 Ingest — nth run

**The watermark problem.** `transaction_id` is a UUID, so it cannot be a
high-water mark. `MAX(transaction_date)` alone is also unsafe: banking systems
**post late**, so a transaction dated the 3rd can arrive on the 9th and a strict
`> watermark` filter loses it permanently.

The fix — pull `transaction_date > (watermark − 7 day lookback)`, then
**anti-join on the primary key**:

```sql
INSERT INTO txn_fact
SELECT … FROM delta d
WHERE NOT EXISTS (SELECT 1 FROM txn_fact f WHERE f.transaction_id = d.transaction_id);
```

Measured: **233ms** to append and dedupe a 50k-row delta.

**New merchant strings must match existing canonicals first.** If clustering
re-runs blind on the delta, `SWIGGY` could form a *second* canonical, silently
splitting one merchant in two and corrupting every historical total. Match new
distinct strings against the **existing** canonical set first; only genuinely
unseen strings create new entries.

**Rebuild the rollup entirely — on purpose.** Measured:

```
append 50k delta + dedupe  : 233.3 ms
FULL rollup rebuild (4.05M):  157.4 ms
```

A full rebuild is *faster than the delta append*, so incremental rollup logic is
not worth writing — it is more code and it can drift out of sync with the facts.
This holds only because the aggregate is four orders of magnitude smaller than
the fact table.

**`account` is a full refresh.** `available_balance` is a mutable snapshot, not
an append-only fact; deltas are meaningless for it. The table is tiny — replace
it.

**The alias map is a slowly-changing dimension.** Adding
`BUNDL TECHNOLOGIES → SWIGGY` later changes **every historical answer**. That is
usually desirable but must be deliberate: the manifest stores an alias-map
version, and bumping it triggers a re-map pass over `txn_fact` (a join, fast)
plus a rollup rebuild. Without versioning, answers quietly differ between runs.

### 12.4 Query — traced end to end

*"How much did I spend on Swiggy last month?"*

| # | Stage | Detail |
|---|---|---|
| 1 | Transport | HTTPS → FastAPI → session resolves `entity_id`. The model never sees it. |
| 2 | Agent turn 1 | Emits `resolve_merchant("swiggy")` + `resolve_time_range("last month")`, dispatched concurrently |
| 3 | Dispatcher | Validates args against the Pydantic schema, injects `entity_id`, executes parameterised SQL on a per-call cursor |
| 4 | Resolution | `merchant_dim` **~2ms** → `MATCH "SWIGGY"`, 14 months history, conf 0.98. Time range anchored to `MAX(transaction_date)` **in the data, not the wall clock** → `2026-05-01 .. 2026-05-31` |
| 5 | Policy gate | Range supplied + merchant unambiguous → no clarification. Had the user said only *"my spending for swiggy"*, gate 1 trips and `ask_user` fires instead |
| 6 | Agent turn 2 | Emits `query_spend(merchant="SWIGGY", start, end, direction="debit")` |
| 7 | Router | Month-aligned → `rollup_monthly`, **6.8ms**. Returns facts: total, count, avg, span, grand total, `truncated` |
| 8 | Agent turn 3 | Writes prose from the facts block. PII redaction, then **numeric verification** — every figure must exist in the facts or the text is discarded for the deterministic template |
| 9 | Response | Answer + table + CSV + audit trace (every tool call, args, SQL, params, rows, latency) |

**Database time for the whole turn: ~50–100ms.** The rest of the ~1s budget is
LLM round trips.

### 12.5 Routing: rollup vs fact — and the honest limitation

The monthly rollup **cannot** answer arbitrary date ranges. "Between the 5th and
the 12th" and "last 30 days" do not align to month boundaries.

| Request | Store | Measured |
|---|---|---|
| Month-aligned window, aggregate | `rollup_monthly` | 6.8 ms |
| Arbitrary window, or individual rows | `txn_fact` | 18.2 ms |

The fallback is ~2.7× slower and still far from a bottleneck, so the router stays
simple and conservative: **use the rollup only when the window is provably
month-aligned; otherwise use the fact table.** Failing the other way — silently
snapping "last 30 days" to month boundaries — is a [B01](BUGS.md)-class wrong
answer.

### 12.6 How clarification suspends and resumes

When `ask_user` is called the turn **ends**; the agent does not block. The UI
renders option chips, and the accumulated tool results (resolved merchant,
partial filters) are stored in the session alongside the pending intent. The
user's reply **resumes** that state rather than restarting, so
`resolve_merchant` does not run twice and the conversation does not lose the
merchant it already pinned down.
