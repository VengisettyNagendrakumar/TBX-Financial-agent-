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
                        │   AGENT (LangGraph DAG)      │
                        │  ≤20B model, function calls  │
                        │  1 model decision per turn   │
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

### 7.3 Control flow — a graph, not a loop

Implemented as a **LangGraph state machine** ([graph.py](graph.py)); the
reasoning behind that choice is [§14](#14-agent-framework-custom-vs-langchain-vs-langgraph).

The turn is a DAG, not an iteration. The model contributes **exactly one
decision** — which tool, with which arguments — and every edge after that is
code:

```
plan ─▶ inherit ─┬─▶ ask_user ──────────────────────────▶ CLARIFY
                 ├─▶ balances ──────────────────────────▶ ANSWER
                 └─▶ resolve_entity ─┬─────────────────▶ CLARIFY / GUARDRAIL
                                     ▼
                               gate_person ────────────▶ CLARIFY
                                     │
                  ┌──────────────────┴──────────┐
                  ▼                             ▼
              compare ─▶ ANSWER         resolve_period ─▶ CLARIFY
                                                │
                                                ▼
                                            execute ─▶ narrate ─▶ ANSWER
```

Two model calls per turn: one to plan, one to narrate. There is no iteration
budget to exhaust, so turn latency is bounded by construction rather than by a
cap — see [§5](#5-latency-plan), where the model is the entire budget and the
database is ~5ms of it.

Five of the nine nodes can terminate the turn. That is why the structure is a
graph: the guardrails *are* the control flow, and conditional edges state them
where a chain would bury them.

Every node appends to an additive `trace` in the graph state — its arguments,
SQL, params, row count and latency — which the audit drawer renders. The
explainability requirement therefore covers the agent's whole decision path, not
just the final SQL, and the trace falls out of the topology instead of being
maintained by hand.

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

---

## 13. Confidence scoring

Implemented in `FinanceAgent._confidence` ([agent.py](agent.py)); rendered as a
band with an expandable reason list in [app.py](app.py).

### 13.1 What confidence does and does not mean

**It is not a probability that the number is right.** Every figure is computed
by SQL, and [explainer.py](explainer.py) discards any model wording containing a
figure the database did not return. Arithmetic error is not a failure mode this
system has.

What genuinely varies is whether the **question was interpreted the way the
user meant it**:

| Interpretation risk | Example |
|---|---|
| Wrong counterparty | "swigy" → SWIGGY, or one of three "SELECTION …" merchants |
| Assumed window | No period given, so the answer covers two years |
| Incomplete attribution | 6% of narration in range is unparseable, so per-merchant figures exclude it |
| Unstable narration | The model emitted an unverifiable figure and was overridden |

So the band answers: *"how likely is it that this answers the question you
actually asked?"* — not *"how likely is this arithmetic to be correct?"*

### 13.2 Why bands rather than a percentage

An earlier build displayed "93% · High". That was withdrawn: the score is a
combination of heuristics, not a measurement, and a two-significant-figure
number invites the reader to treat it as one — to wonder what changed between
91% and 93% when nothing meaningful did. Three bands carry the actionable
distinction and nothing more:

| Band | Score | Means | What to do |
|---|---|---|---|
| **High** | ≥ 0.88 | Interpreted unambiguously | Use it |
| **Medium** | 0.72 – 0.88 | One or more details were assumed | Skim the *Interpreted as* line |
| **Low** | < 0.72 | Several assumptions compounded | Verify before relying on it |

The numeric score is retained internally for logs and tests (`Confidence.pct`)
but is never displayed.

### 13.3 The four signals

Each signal produces a factor in `[0, 1]`. The factors are **multiplied**:

```
score = resolution × period × attribution × narration
```

Multiplication rather than "worst signal wins" is deliberate. A fuzzy-matched
counterparty over an assumed window with patchy attribution should not read as
confidently as any one of those problems alone — mild doubts must compound.

**1 · Counterparty resolution** — the resolver's own confidence, taken directly.

| How the name matched | Factor | Reason shown |
|---|---|---|
| Exact canonical name | 1.00 | *Matched 'SWIGGY' exactly* |
| Brand/legal alias (`BUNDL TECHNOLOGIES`) | 0.98 | *Matched 'SWIGGY' by alias …* |
| Acronym | 0.96 | *… by acronym …* |
| Whole-word containment (`apollo` → `APOLLO PHARMACY`) | 0.95 | *… by contained …* |
| Substring | 0.92 | *… by substring …* |
| Fuzzy / typo (`swigy`) | its score, 0.88–0.92 | *… by fuzzy rather than an exact name* |
| No counterparty in the question | 1.00 | — |

Ambiguous and not-found names never reach scoring — they become a clarifying
question or a guardrail instead.

**2 · Period assumption**

| Situation | Factor | Reason shown |
|---|---|---|
| Explicit window ("last month", "in April") | 1.00 | *Period resolved to May 2026* |
| User explicitly said "total" / "all time" | 1.00 | *Answered over all available history, as asked* |
| **No period mentioned at all** | **0.92** | *No time period given — answered over all available history* |

The distinction matters: asking for a total is a stated intent; saying nothing
means the system chose a two-year window on the user's behalf.

**3 · Data attribution** — `1.0 − unattributed_share`, where the share is the
proportion of in-scope transactions whose counterparty could not be parsed from
the narration (`merchant_norm = 'UNKNOWN'`), computed over the same entity,
direction and window as the answer. Applied only above 2%, to avoid noise.

> Reason shown: *"6% of transactions in this window have no identifiable
> counterparty and cannot be attributed to a merchant"*

This is the confidence-layer expression of the coverage measurement from §4.2 —
the ~6% of narration that is genuinely opaque (`TRF/27964914/15335598`) is
disclosed rather than silently excluded.

**4 · Narration integrity**

| Situation | Factor | Reason shown |
|---|---|---|
| Model wording passed verification | 1.00 | — |
| Model wording **rejected** | **0.85** | *The model produced a figure the database did not return; its wording was discarded and a verified summary used* |

The displayed number is still correct — the deterministic template replaced the
prose. The penalty reflects that the model was behaving unreliably on this turn,
which is weak evidence it also mis-framed something unverifiable.

### 13.4 What is deliberately *not* penalised

**Truncation.** Showing 10 of 40 counterparties does not reduce confidence,
because the reported total covers all 40 — the answer is complete, only the
table is capped. It *is* surfaced as a reason, since a reader who assumes the
table is the whole story would misread it:

> *Showing 10 of 40 rows — the total covers all of them*

**Result size.** A window with few transactions is not less trustworthy; it is
a smaller number, exactly computed.

**Model vs rules planner.** Both emit the same validated tool arguments, and the
rules planner is arguably the more predictable of the two. Which one ran is in
the audit trace, not the confidence.

### 13.5 Fixed-confidence paths

Two answers bypass scoring because there is nothing to interpret:

- **Balances** — read straight from the account record. Always High: *"Balance
  read directly from the account record"*.
- **Not-found guardrails** — "I have no transactions for Oracle" is a confident
  statement of absence, not a low-confidence answer. Always High.

### 13.6 Worked examples

| Question | Factors | Score | Band |
|---|---|---|---|
| "How much on Swiggy last month?" | 1.00 × 1.00 × 0.93 | 0.93 | **High** |
| "Which vendor have I spent most on?" | 1.00 × 0.92 × 0.94 | 0.87 | **Medium** |
| "How much on swigy last month?" (typo) | 0.91 × 1.00 × 0.93 | 0.85 | **Medium** |
| "How much on swigy?" (typo, no period) | 0.85 × 0.92 × 0.90 | 0.70 | **Low** |
| "What's my balance?" | fixed | 1.00 | **High** |

### 13.7 Limitations

- The weights are **calibrated by judgement, not fitted to data**. There is no
  labelled set of correct-vs-misinterpreted answers to fit against; building one
  is the honest next step.
- The bands are **ordinal, not calibrated** — "High" does not assert a 90%
  success rate.
- Attribution share is measured over the whole window, not per counterparty, so
  it is a property of the data rather than of this specific answer.
- Confidence covers **interpretation only**. It says nothing about whether the
  underlying source data is complete or correct.

---

---

## 14. Agent framework: custom vs LangChain vs LangGraph

**Decision: a LangGraph state machine over a closed set of typed tools, with a
single model-authored decision per turn.**

Orchestration lives in [graph.py](graph.py); the domain logic it calls
(resolution, gates, confidence) stays in [agent.py](agent.py). All **153 tests
passed unchanged on the first run** after the swap, which is the evidence that
the migration was behaviour-preserving rather than a rewrite.

This section records why, in enough detail to defend under questioning.

---

### 14.1 The two decisions

They are separable, and conflating them is the usual source of confusion:

1. **How does the model touch the database?** → *Tool calls with typed
   arguments*, not generated SQL.
2. **What drives control flow between steps?** → *An explicit graph*, not a
   model-driven loop and not implicit control flow.

Different constraints motivate each.

---

### 14.2 Decision 1 — tool calls, not text-to-SQL

The obvious alternative is letting the model write SQL against the schema. We
rejected it on four grounds, three of which are hard requirements rather than
preferences.

| | Text-to-SQL | Typed tool calls (chosen) |
|---|---|---|
| **Tenancy** | Model must be trusted to include `WHERE entity_id = …` | `entity_id` injected server-side; unforgeable |
| **Injection** | Model output *is* the query | Model picks a tool; args bound as `?` parameters |
| **Cost ceiling** | Model can emit a cross join over 4M rows | Query shapes are fixed and benchmarked |
| **Verifiability** | Must validate arbitrary SQL to trust it | Finite, individually tested surface |
| **Recovery** | Syntax/semantic errors need a repair loop | Invalid args rejected before execution |

The tenancy point is the decisive one. §6.4 states the invariant: **the model
chooses what to filter, never whose data to read.** With generated SQL that
invariant can only be enforced by parsing and rewriting the model's output —
you are then validating a Turing-complete language on the security boundary. A
tool signature makes it structural: `entity_id` is not a parameter the model can
express.

The cost argument matters at our scale specifically. §5 shows the rollup path is
0.8ms and the fact-table fallback 2.0ms *because our code chooses the store
based on month-alignment*. A model authoring SQL would not reliably make that
routing choice, and an unconstrained query over 4M rows is unbounded in a way a
sub-second budget cannot absorb.

The residual cost of tool calling is expressiveness: we can only answer
questions someone has built a tool for. That is an accepted trade — the brief
explicitly scopes to "a well-scoped subset (spend, payouts, reconciliation)",
and an assistant that answers eight things correctly beats one that answers
thirty unreliably in a domain where a wrong number is a liability.

#### 14.2.1 The sandboxed SQL fallback — and why it does not reverse the decision

Live testing surfaced a long tail the eight typed tools could not express:
"which transaction is more than one lakh", "how many transactions on weekends",
"on which month was my expense highest". The first instinct — add a tool per
shape — does not scale. The second — let the model write SQL — is exactly what
§14.2 rejects. The resolution is a **fallback, not a default**, with every
§14.2 objection answered structurally rather than by prompt:

| §14.2 objection | How the fallback answers it |
|---|---|
| Tenancy must not depend on the model | The model can only name two views, `my_transactions` and `my_accounts`, created **per request** with `WHERE entity_id = <session>` baked into their definition. The validator refuses any other table name. There is no SQL the model can write that reads another customer's rows. |
| Model output is the query | Literals travel as bound `?` parameters. The SQL text is parsed by DuckDB's own parser (`json_serialize_sql`) **without executing**; anything other than exactly one `SELECT` over the two views is refused — including table functions such as `read_parquet`. |
| Unbounded cost | A `LIMIT` of 200 is enforced on every query. |
| Verifiability | The query is shown in the audit trace; numeric verification of the narration still applies. |
| PII | `utr_number` and raw `account_number` are simply not columns of the views; `description` is redacted in results. The model sees the schema, never rows. |

Two further guarantees were added because the sandbox alone left them open:

- **Entity resolution cannot be bypassed.** A name the model filters on —
  as a parameter *or* an inlined literal — goes through the same resolver the
  typed tools use. `Oracle` yields the standard *"no such vendor"* guardrail,
  `selection` asks which of three, `swiggy` is substituted with its canonical.
  Without this, `WHERE merchant = 'ORACLE'` would return zero rows and be
  narrated as "you spent nothing" — a false statement with no false *number*,
  which numeric verification cannot catch.
- **The band goes down and says why.** The sandbox guarantees scope and safety;
  it cannot guarantee the model understood the question, because no typed
  contract constrained the mapping. Answers produced this way carry a
  `VIA_SQL_FACTOR` and a reason pointing at the query.

The typed tools remain primary: the prompt directs the model to them whenever
one fits, and several of the reported failures were in fact fixed by
*extending* them (`order_by`, `min_amount`, `group_by_month`, calendar years,
"first/last N") rather than by SQL. The fallback exists for the shapes that
remain. The rules planner — no model — cannot use it, and labels un-appliable
filters honestly rather than answering a different question.

---

### 14.3 Decision 2 — why a graph

**The shape of the problem is a graph, not a chain.** Five of the nine nodes can
end the turn:

| Exit | Trigger | Why it must be guaranteed |
|---|---|---|
| `CLARIFY` | counterparty matches several names | Silently picking one invents a number |
| `CLARIFY` | "my friend" — one unnamed person | Totalling everyone answers a different question |
| `CLARIFY` | period given but unparseable | Dropping it silently widens scope (BUGS.md B01) |
| `CLARIFY` | no period, counterparty spans months | The brief's own worked example |
| `GUARDRAIL` | counterparty absent from history | Hallucination guardrail is a Must-Have |

A chain models "A then B then C". This is "A, then *maybe stop*, then B, then
*maybe stop*". Conditional edges say that directly; the guardrails are the part
of this system that most needs to be legible, because they are what stops a
confident wrong answer reaching a finance user.

---

### 14.4 Why not LangChain's agent runtime

`AgentExecutor` / `create_react_agent` run a **ReAct loop**: the model observes,
decides the next action, and decides when to stop. Four problems here, in
descending order of severity.

**1. Policy gates become unenforceable.** Our hard requirement is that certain
conditions *always* produce a clarifying question. In a ReAct loop the decision
to ask is the model's. You can prompt for it; you cannot guarantee it. We have
direct evidence this matters: while testing the "my friend" gate, the planner
chose `get_spend` on one turn and `rank_counterparties` on another for the same
question. Because the gate ran *outside* the model's control, both were caught.
Under a ReAct loop that variance would have surfaced as an occasionally-wrong
answer — the worst failure mode, because it is intermittent.

**2. Latency is unbounded.** A ReAct loop is typically 3–6 model round trips.
Ours is exactly two: one to choose a tool, one to narrate. At ~300ms each
(§5) that is the difference between a ~0.7s turn and a 2–4s turn, against a
sub-second target. The database work is ~5ms; the model is the entire budget,
so the number of calls *is* the latency design.

**3. Cost is unbounded per turn.** Relevant under a capped token allowance —
and we exhausted a 200k/day Groq quota during development.

**4. Testability.** Node functions are directly callable. Loop-internal
behaviour has to be tested by running the loop and asserting on outcomes, which
makes failures harder to localise.

**A ReAct loop is the right choice when the task requires open-ended
exploration** — unknown numbers of steps, tool outputs that determine the next
tool. Our turn has a known shape. Paying for a loop we do not need would buy
non-determinism.

---

### 14.5 Why not LCEL chains

LangChain's expression language composes linear pipelines well. Ours branches
five ways and accumulates state (`canonical`, `time_range`, `resolution`,
`trace`) that later nodes read. `RunnableBranch` can express branching, but
nested branches obscure exactly the structure we want visible, and LCEL has no
first-class notion of *pausing a run and resuming it later* — which is precisely
the clarify/answer cycle.

---

### 14.6 Why not stay custom

The honest position: **the custom loop worked.** It passed the same 153 tests.
This was not a bug fix, and it should not be defended as one.

What the graph buys:

| Property | Custom | LangGraph |
|---|---|---|
| Routing rules | Re-derived at each `return`, inside a 160-line function | Declared once, in one place |
| Adding a guardrail | Find the right point among nested returns | Add a node + an edge |
| Testing a step | Run a whole turn | Call the node |
| Pause / resume | Hand-rolled `pending` dict | Checkpoint + interrupt (standard) |
| Showing the design | Prose description | `graph.ascii_diagram()` renders the real topology |
| Audit trace | Manual `trace.append` bookkeeping | One entry per node, structurally |

The clarify/resume cycle is the strongest case. We solved it by hand: serialise
a `pending` dict, stash it in `session_state`, reconstruct arguments on the next
turn. That is a re-implementation of checkpoint-and-interrupt, and ours is the
weaker version — it dies on page refresh (§14.9).

**Migration cost was low precisely because the domain logic was already
extracted into helpers.** The nodes are thin wrappers. Had the logic been inline
in the loop, this would have been a rewrite, and the honest recommendation would
have been to leave it alone.

---

### 14.7 What LangGraph we deliberately do not use

Stating this pre-empts the obvious objection — *"you pulled in a framework and
used 10% of it"* — which is true and intentional.

- **`create_react_agent`** — the prebuilt ReAct agent. Not used, for §14.4.
- **Multi-agent / supervisor patterns.** One agent, one domain. Multiple agents
  would add coordination failure modes and latency for no capability.
- **Cyclic edges.** Our graph is a DAG. Cycles are how a ReAct loop is built;
  we do not want one.
- **`langchain-core` abstractions.** It arrives as a transitive dependency, but
  no chain, model wrapper, memory, retriever or output parser is used. Groq is
  called directly with its own SDK.

So the model contributes **exactly one decision per turn**: which tool, with
which arguments. Every edge after that is code. That is the property that makes
the guardrails guarantees rather than tendencies.

---

### 14.8 Costs we accept

| Cost | Assessment |
|---|---|
| `langgraph` + `langchain-core` dependency | Real. Justified by checkpointing and the routing structure; both are pure-Python and stable. |
| TypedDict state boilerplate | ~40 lines. Buys type-checkable state instead of ad-hoc locals. |
| Framework churn risk | LangGraph's API has moved historically. Mitigated: our surface is `StateGraph`, `add_node`, `add_conditional_edges`, `compile`, `invoke` — the stable core — and domain logic is outside it, so a rewrite of `graph.py` would not touch the rules. |
| Indirection for readers | A reader must follow node → helper. Mitigated by the topology comment at the top of `graph.py`. |

---

### 14.9 Not yet taken up

Available, unused, and worth naming so the dependency is honestly accounted for:

- **Checkpointer** — clarification state still lives in Streamlit
  `session_state`, which is why history is lost on page refresh. A checkpointer
  persists it and makes resume durable. Phase 5.
- **Streaming** — nodes could emit progress ("resolving counterparty…",
  "querying…") instead of one spinner.
- **`interrupt()`** — would express the clarification pause natively rather than
  via terminal state plus a `pending` dict.
- **LangSmith tracing** — would subsume the hand-built trace, though ours is
  deliberately user-facing, not a developer tool.

---

### 14.10 When we would choose differently

Judgement is easier to trust when it names its own limits:

- **Open-ended research questions** ("find anything unusual in my spending") →
  a ReAct loop, because the number of steps genuinely is not known ahead.
- **A schema too large to wrap in tools** (hundreds of tables, arbitrary
  analytics) → text-to-SQL with a validation layer, accepting the cost.
- **A prototype with no guardrail requirements** → stay custom; the framework
  would not pay for itself.
- **Many specialised domains** (tax, investments, lending) → multi-agent
  supervision, where coordination cost buys real separation.

None describes this system: a fixed schema, a bounded question set, and hard
requirements on clarification and grounding.

---

### 14.11 One-paragraph defence

> The finance domain makes wrong answers liabilities, so the brief requires
> guardrails and clarifying questions. A guarantee cannot be delegated to a
> model's judgement, so control flow must be code, not a ReAct loop — which also
> keeps us at two model calls per turn inside a sub-second budget. Tool calls
> rather than generated SQL make tenancy scoping structural instead of prompted,
> and bound the cost of any single query at 4M rows. LangGraph expresses that
> control flow as nodes and conditional edges, which matters because five of
> nine nodes are early exits — the guardrails *are* the structure. We use the
> graph and nothing else from the ecosystem: the model contributes exactly one
> decision per turn, and every edge after it is code.
