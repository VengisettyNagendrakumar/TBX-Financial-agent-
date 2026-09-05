# Grounded Finance Assistant

**TBX — BVP Tech Catalyst Hackathon**
*Build a Finance Assistant That Actually Understands You*

Ask about your money in plain language. Every figure is computed by SQL; the
model explains results and **cannot emit a number the database did not produce**.

```
You   How much have I spent on Swiggy last month?
      ₹127,896.90 across 28 transactions in May 2026, averaging ₹4,567.75.
      ● High confidence — interpreted unambiguously

You   Show me these transactions
      Found 28 transactions totalling ₹127,896.90 in May 2026.
      ↩ Carried over: merchant = SWIGGY, period = last_month

You   compare it to the 3 months before
      Apr–Jun 2026: ₹306,936.70 vs Jan–Mar 2026: ₹437,823.80 — down ₹130,887.10 (29.9%)

You   I want to calculate my spending for swiggy
      I have 24 months of activity for SWIGGY (2024-07 to 2026-06). Which period?
      [Last month] [Last 3 months] [Last 6 months] [This year] [All time]

You   how many transactions did I make on weekends
      You made 5,779 transactions on weekends.        ◐ Medium — answered with sandboxed SQL

You   What did I spend on Oracle?
      I have no transactions for Oracle. Closest names on record: OLA, KARAN MALHOTRA, RELIANCE DIGITAL.
```

**Status:** 256/256 tests · 24/24 on the question set for both the model planner
and the no-model fallback · every number below was measured, not estimated.

---

## Contents

1. [Setup](#1-setup)
2. [Architecture — high level](#2-architecture--high-level)
3. [Architecture — low level](#3-architecture--low-level)
4. [Data flow: ingest](#4-data-flow-ingest)
5. [Data flow: a query](#5-data-flow-a-query)
6. [Prompt engineering principles](#6-prompt-engineering-principles)
7. [Guardrails](#7-guardrails)
8. [Handling ambiguous questions](#8-handling-ambiguous-questions)
9. [Observability](#9-observability)
10. [Model choice & efficiency](#10-model-choice--efficiency)
11. [Trade-offs](#11-trade-offs)
12. [Performance](#12-performance-measured-4m-rows)
13. [Project layout](#13-project-layout)
14. [Known gaps](#14-known-gaps)

---

## 1. Setup

**Requires Python 3.10+** (developed on 3.14).

```bash
pip install -r requirements.txt
```

### Model provider (optional)

The app is fully functional with no API key — a deterministic planner and
template narration take over. With a key you get model-written phrasing and the
long-tail SQL fallback. Any OpenAI-compatible endpoint works; switching is a
`.env` edit, never a code change:

```bash
# .env
LLM_BASE_URL=https://api.groq.com/openai/v1    # Groq (Section 7 compliant model below)
LLM_MODEL=openai/gpt-oss-20b
GROQ_API_KEY=...

# or
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=...
```

Provider-specific quirks (`reasoning_effort`, `max_completion_tokens`,
temperature restrictions) are learned at runtime per model — one wasted request,
once — rather than hardcoded.

### Run with demo data

```bash
python data_generator.py --rows 4000000     # ~12s; --rows 200000 to go faster
python build_warehouse.py                   # ~13s at 4M rows
streamlit run app.py
```

The generator produces synthetic Indian bank narration in the formats from the
organisers' sample (UPI / NEFT / IMPS / FT / RTGS), deliberately including
brand-vs-legal-name variants, trailing city names, bank charges and a slice of
genuinely unparseable text — so extraction coverage is a real measurement rather
than 100% by construction.

### Run against the organiser's database

```bash
# 1. paste the URL into DATABASE_URL at the top of datasource.py
python ingest.py --check      # connect + validate schema, writes nothing
python ingest.py --purge      # delete the demo data, import theirs
streamlit run app.py
```

Run `--check` first: it verifies the connection and every column we read
*without deleting anything*, and names the file to edit if a table or column
differs. `--purge` matters: without it an import **adds to** the demo data and
answers would mix real and synthetic transactions.

| Flag | Use |
|---|---|
| `--url "mysql://…"` | URL on the command line instead of the file |
| `--limit 100000` | Smoke-test on a slice first |
| `--incremental` | Top up an existing warehouse |
| `--allow-insecure` | Only if their endpoint has no TLS (warns every time) |
| `--purge-only` / `--purge-chats` | Wipe demo data / saved conversations |

### Tests and evaluation

```bash
python test_warehouse.py      # 256 assertions — values, not shapes
python eval_agent.py          # 24-question interpretation benchmark, both planners
python eval_agent.py --model llama-3.1-8b-instant   # benchmark another model
```

> **In short**
> - One `pip install`, two commands to build demo data, one to run.
> - Works with **no API key**; a key adds fluent phrasing and the SQL fallback.
> - Provider is chosen by `LLM_BASE_URL` — Groq, OpenAI, Together, local — with quirks learned at runtime.
> - Live ingest is a two-step `--check` then `--purge`, so the schema is validated before anything is deleted.

---

## 2. Architecture — high level

### The constraint that shapes everything

The schema is three tables — `bank`, `account`, `transaction` — and **no vendor
table**. "Swiggy" exists only inside free-text narration:

```
UPI-NAVYUG SELECTION-XXXXXX8672-AUBL0002125-103293775381-260514201735136
NEFT  - UTIB0002678 - 95604250 - 915020031685136 - UMANG SELECTION
IMPS/P2A/600228462725/UTIB/918020101986700/00/INET/9211/SELECTIONMALIGAI/…
FT -  95842568 -  50200013729069 - SELECTION ELECTRONICS   DAHISAR EAST
```

Two consequences: **"which vendor did I spend the most on?" is impossible without
a grouping key** (you cannot `GROUP BY` a substring you never extracted), and
**`LIKE '%swiggy%'` cannot be indexed**. So the counterparty dimension is
**derived once at ingest**, not parsed per query.

### The shape

```
 ┌───────────────┐   TLS    ┌──────────────────────────────────────────────────┐
 │ Organiser DB  ├─────────▶│ INGEST  land → extract → normalise → map → roll up│
 │ (MySQL/PG/…)  │          └───────────────┬──────────────────────────────────┘
 └───────────────┘                          ▼
                           ┌──────────────────────────────────────┐
                           │ DuckDB warehouse (read-only at query)│
                           │  merchant_dim   the vocabulary       │
                           │  rollup_monthly the answers          │
                           │  txn_fact       the evidence         │
                           └───────────────▲──────────────────────┘
                                           │ parameterised SQL only
 ┌───────────────┐  session   ┌────────────┴───────────────────────┐
 │ Streamlit UI  ├───────────▶│ AGENT — LangGraph DAG, 11 nodes    │
 │ chats.db      │ entity_id  │  model: ONE decision per turn      │
 │ (threads)     │◀───────────┤  (tool + args); every edge is code │
 └───────────────┘  answer +  │  gates · resolver · sandbox ·      │
                    trace     │  narrator · verifier · confidence  │
                              └────────────────────────────────────┘
```

Three invariants hold everywhere:

1. **The model never writes SQL and never does arithmetic.** It selects a typed
   tool and fills arguments. Totals, averages, deltas — all computed in SQL.
2. **`entity_id` is injected server-side.** The model chooses *what* to filter,
   never *whose data* to read.
3. **Every number in the answer is verified against computed facts** before it
   is shown; an ungrounded figure discards the model's wording for a template.

> **In short**
> - No vendor table exists, so the counterparty dimension is **derived at ingest** — this one fact drives the data layer.
> - Three stores with three jobs: vocabulary (`merchant_dim`), answers (`rollup_monthly`), evidence (`txn_fact`).
> - The agent is a graph where the model makes exactly **one decision per turn**; everything after it is code.
> - Tenancy, arithmetic and grounding are structural guarantees, not prompt requests.

---

## 3. Architecture — low level

### Modules

| Layer | File | Responsibility |
|---|---|---|
| Ingest | `datasource.py` | URL parsing, TLS, DuckDB `ATTACH` for MySQL/Postgres/SQLite/DuckDB/files, schema validation |
| | `ingest.py` | `--check` / `--purge` / `--incremental` CLI; delegates the build |
| | `build_warehouse.py` | Full and incremental builds; watermark + lookback; manifest |
| | `enrichment.py` | Narration → counterparty (SQL), vocabulary clustering (Python), rollups |
| Storage | `db.py` | Connections, cursors, time-range grammar (`resolve_time_range`, `previous_window`), manifest |
| | `config.py` | Schema map, aliases, enums, model/provider settings — the single source of truth |
| Query | `queries.py` | Deterministic parameterised SQL; every result carries `facts`, `truncated`, `notes` |
| | `sqlguard.py` | The sandbox for model-written SQL (§7) |
| | `resolver.py` | Counterparty → `MATCH` / `AMBIGUOUS` / `NOT_FOUND` |
| Agent | `agent.py` | Tool schemas, both planners, policy gates, context inheritance, confidence |
| | `graph.py` | The LangGraph state machine — nodes, routing, per-turn side-channel |
| | `explainer.py` | Narration prompt, deterministic templates, numeric verification, `describe_scope` |
| | `llm.py` | Provider shim over the OpenAI SDK; learns per-model quirks |
| Session | `session.py` | The logged-in customer (hardcoded; env-overridable) and primary account |
| | `security.py` | Masking, PII redaction, UTR blind index |
| | `chatstore.py` | Conversations + LangGraph checkpoints in SQLite |
| UI | `app.py` | Streamlit chat, conversation list, clarification chips, audit drawers |

### Warehouse tables

| Table | Grain | Rows at 4M | Sorted by | Purpose |
|---|---|---|---|---|
| `raw_bank`, `raw_account`, `raw_transaction` | source | as loaded | — | landed copies; `account` is always fully refreshed |
| `txn_fact` | one transaction | 4,000,000 | `(entity_id, merchant_norm, transaction_date)` | evidence; derived `merchant_norm`, `counterparty_kind`, `channel`, `txn_month` |
| `rollup_monthly` | entity × merchant × month × type | 368,136 | `(entity_id, merchant_norm, txn_month)` | answers; `SUM`, `COUNT`, `MIN`, `MAX` |
| `merchant_dim` | entity × merchant | 11,000 | — | the resolver's candidate list |
| `merchant_alias` | raw string → canonical | ~230 | — | preserved across incremental loads so a merchant never splits |
| `ingest_manifest` | one row | 1 | — | watermark, row count, schema hash, alias-map version |

Physical sort order is what makes queries fast: DuckDB prunes with zone maps,
not B-tree indexes (an ART index was measured and did not help).

### The agent graph

Eleven nodes, deterministic routing, five of which can end the turn early:

```
plan ─▶ inherit ─┬─▶ ask_user ───────────────────────────────▶ CLARIFY
                 ├─▶ balances ───────────────────────────────▶ ANSWER
                 ├─▶ generated_sql ──────────────▶ ANSWER / GUARDRAIL / CLARIFY
                 └─▶ resolve_entity ─┬──────────────────────▶ CLARIFY / GUARDRAIL
                                     ▼
                               gate_person ─────────────────▶ CLARIFY
                                     │
              ┌──────────────────────┴──────┐
              ▼                             ▼
          compare ─▶ ANSWER         resolve_period ──────────▶ CLARIFY
                                            │
                                            ▼
                                        execute ─▶ narrate ─▶ ANSWER
```

| Node | Does |
|---|---|
| `plan` | Model tool call (or rules planner); resumes a pending clarification |
| `inherit` | Follow-up context; all-time normalisation; named-person reroute |
| `ask_user` | A model-initiated clarification |
| `balances` | Primary account, or all accounts on request |
| `generated_sql` | Sandboxed fallback with counterparty guard |
| `resolve_entity` | Direction; counterparty resolution; time-phrase and clause guards |
| `gate_person` | "my friend" → which person? |
| `compare` | Two windows; baseline derived if absent |
| `resolve_period` | Period grammar; the period gate; bounded-listing bypass |
| `execute` | The typed query (spend / list / rank) |
| `narrate` | Facts → prose → numeric verification → confidence |

### State and the side-channel

Graph state is **plain JSON only**. Rich objects — `QueryResult` (holds a
DataFrame), `Resolution`, `TimeRange`, `Confidence` — live in
`agent._scratch[run_id]` for the duration of one turn and never reach the
checkpointer. An earlier build pickled them into checkpoints; the first time
Streamlit hot-reloaded `queries.py`, pickle resolved the class by import path,
found a different class object, and **every turn failed** ("not the same object
as `queries.QueryResult`"). The suite now asserts no checkpoint is pickle-typed,
including across a simulated module reload.

Conversations are keyed by a LangGraph `thread_id`; each turn runs under a
derived `conversation#turn` thread so turns start clean while a conversation's
checkpoints stay grouped and deletable together.

> **In short**
> - Sixteen small modules with one job each; `config.py` is the single source of truth for schema and provider.
> - Six warehouse tables; the two hot ones are physically sorted so zone maps prune — an index did not help.
> - An 11-node LangGraph DAG with deterministic routing; five nodes can end the turn early.
> - Checkpointed state is primitives only; rich objects ride a per-turn side-channel, which is what survives hot-reloads.

---

## 4. Data flow: ingest

### First run

```
①  land       ATTACH the source over TLS; CREATE TABLE raw_* AS SELECT …          1.9 s
②  extract    narration → merchant_raw, channel (SQL CASE/split_part, vectorised)  6.0 s
③  normalise  ~3,000 DISTINCT strings → canonical (Python, fuzzy + alias map)       2.2 s
④  map+sort   join alias table; write txn_fact sorted (entity, merchant, date)      6.1 s
⑤  aggregate  rollup_monthly, merchant_dim; write manifest                          0.9 s
```

Tracing one real sample row through ②:

```
'FT -  95842568 -  50200013729069 - SELECTION ELECTRONICS   DAHISAR EAST'
  channel        ← prefix 'FT '                 → FT
  split ' - '    → [FT, 95842568, 50200013729069, SELECTION ELECTRONICS   DAHISAR EAST]
  counterparty   ← most name-like field         → 'SELECTION ELECTRONICS   DAHISAR EAST'
  strip location ← text after 3+ spaces         → 'SELECTION ELECTRONICS'
  entity_id      ← join account                 → f2f5e332-…
  txn_month      ← date_trunc('month')          → 2026-06-01
```

Extraction is generic, not per-rail: fixed field positions drift between banks
(IMPS/P2A puts the name at field 9, IMPS OW at field 3). A field survives when it
is purely alphabetic, 3+ chars, not a rail keyword, not an IFSC bank code (read
from the bank table — without this, `HDFC` beat `OLA` and `JIO` on 46,348 rows),
and not a masked account.

**Step ③ is the load-bearing trick.** 4M rows contain ~3,000 distinct merchant
strings — one per 1,333 rows. Fuzzy-clustering 3,000 strings is trivial;
clustering 4M is impossible. *Normalise the vocabulary, not the rows.*
Unparsed rows become `UNKNOWN` and are **reported as unattributed**, never
dropped; coverage is a first-class metric (94.0% on the synthetic set).

### Nth run (`--incremental`)

- **Watermark with a 7-day lookback**, then anti-join on the primary key. Banks
  post late — a transaction dated the 3rd can arrive on the 9th — so a strict
  `> watermark` filter would lose it forever. Measured: 233 ms for a 50k delta.
- **New strings match existing canonicals first.** Otherwise `SWIGGY` in the
  delta forms a second canonical and silently splits every historical total.
- **The rollup is rebuilt wholesale** — measured at 157 ms for 4M rows, faster
  than appending the delta, and it cannot drift out of sync with the facts.
- **`account` is fully refreshed**: `available_balance` is a mutable snapshot,
  not an append-only fact.
- **Alias-map version is stored.** Changing brand↔legal mappings retroactively
  changes past answers; a version bump triggers a re-map, deliberately.

> **In short**
> - Ingest is ~13 s for 4M rows; the expensive parsing runs as vectorised SQL, not a Python loop.
> - Vocabulary normalisation on ~3,000 distinct strings is what makes fuzzy clustering feasible at all.
> - Incremental loads use a lookback + PK dedupe for late postings and preserve existing canonicals so merchants never split.
> - Coverage (94%) is measured and disclosed; unattributed rows lower confidence rather than disappearing.

---

## 5. Data flow: a query

Tracing *"How much have I spent on Swiggy last month?"*:

| # | Step | What happens | Latency |
|---|---|---|---|
| 1 | Session | `entity_id` and primary account resolved server-side from `session.py`; never model-supplied | — |
| 2 | `plan` | Model emits one tool call: `get_spend(merchant="swiggy", period="last_month", direction="debit")`. Schema for the SQL fallback travels in the system prompt — columns only, no rows | ~1.2 s |
| 3 | `inherit` | Not a follow-up; nothing carried. "total/overall" would force `all_time` here | <1 ms |
| 4 | `resolve_entity` | `swiggy` → `SWIGGY` (exact, 1.0). `Swiggy Ltd`, `swigy`, `BUNDL TECHNOLOGIES` all land here too | 0.8 ms |
| 5 | `gate_person` | Not a person question; passes | — |
| 6 | `resolve_period` | `last_month` → 2026-05-01..05-31, anchored to `MAX(transaction_date)`=2026-06-24, **not today's date**; explicit, so the period gate passes | <1 ms |
| 7 | `execute` | Month-aligned → `rollup_monthly`. Facts computed over the **full** set: total, count, average, span | 5.3 ms |
| 8 | `narrate` | Model writes 2–3 sentences from FACTS + INTERPRETATION + 8 redacted sample rows; every number verified; confidence computed | ~1.2 s |
| 9 | UI | Answer, band, interpretation line, metric tiles, table, CSV, audit trace; turn persisted to `chats.db` | — |

**Store routing** is conservative: the rollup is used only when a window is
*provably* month-aligned; "last 30 days" or "5th to 12th" fall back to
`txn_fact` (8.9 ms) rather than being snapped to month boundaries — a silently
wrong answer is worse than a slightly slower right one.

**Follow-ups** ("show me these", "what about April?") inherit counterparty,
period *and* direction from the previous turn — but only with positive evidence
(anaphora or an elliptical fragment). A widening question ("what was my last
transaction *in general*") resets scope and even strips a counterparty the model
carried over on its own.

**Clarifications** end the turn: the UI renders options, the original question
and partial arguments are stored in `pending`, and the reply resumes with the
slot filled. The narrator is given the *original* question plus the resolved
scope, not the two-word reply.

> **In short**
> - Database work is ~1% of a turn (5–11 ms); the model's two calls are the rest, which is why call count is the latency design.
> - Relative dates anchor to the data's last transaction, never the wall clock.
> - The rollup answers only provably month-aligned windows; anything else goes to the fact table rather than being snapped.
> - Follow-ups inherit on positive evidence only; generic questions reset scope.

---

## 6. Prompt engineering principles

There are two prompts — the **planner** (question → one tool call) and the
**narrator** (facts → prose). Both follow the same principles.

**1. The prompt suggests; code guarantees.** Every important behaviour the
prompt asks for also has a deterministic backstop: "ask when unsure" is a policy
gate; "use all_time for *total*" is a normalisation step in `inherit`; "never
use SQL for a named merchant" is a counterparty guard in the sandbox. Prompting
alone is a hope — the same question routed to `rank_counterparties` on one run
and `get_spend` on the next during testing, and the gates caught both.

**2. Anchor time explicitly.** The planner is told the dataset's most recent
date and to resolve relative periods against it. Otherwise "last month" means
last month on the wall clock — a window with zero rows.

**3. Closed vocabularies for anything that becomes a filter.** Periods are
canonical tokens (`last_3_months`, `year_2026`, `all_time`) with a synonym table
and normalisation, and an unrecognised token becomes a *question*, never a
silent all-time query.

**4. Contrast the confusable cases in the prompt.** "*my friend* paid me" (no
name → ranking of persons) vs "*Gautam Singh* paid me" (named → single total);
"highest *transaction*" (one row) vs "which *vendor* did I spend most on"
(ranking); "compare to *the 3 months before*" (omit the baseline; it is derived)
— each added after a real misroute.

**5. Schema for the SQL fallback is columns and enums only — never rows.** The
model can write a query without having seen data. Units it needs (1 lakh =
100000) are spelled out; the two views it may reference are named; everything
else is refused by the sandbox regardless of what the prompt said.

**6. The narrator is handed the answer, not the evidence.** It receives FACTS
computed over the full result, an INTERPRETATION line stating the resolved scope
("counterparty = ALL; period = all time; direction = debit"), and a sample
explicitly labelled partial. It once named the wrong month from an 8-row sample,
and once said "no transactions with Swiggy" after the user had *dropped* the
Swiggy filter — both were fixed by telling it what the data covers instead of
letting it infer from wording.

**7. Copy figures verbatim.** Amounts are pre-formatted (`₹127,896.90`) before
they reach the narrator; it is told to copy them exactly; and verification
strips separators so formatting never weakens the check.

**8. Small, cheap, and two calls.** `reasoning_effort="low"` where supported
(measured 4× faster); `max_tokens` capped; exactly one planning call and one
narration call per turn. A ReAct loop's 3–6 calls would triple latency and halve
the free-tier demo budget.

> **In short**
> - Every prompt instruction that matters has a code-level backstop; the prompt is documentation of intent, not the enforcement.
> - Time is anchored to the data; filters use closed vocabularies; unknowns become questions.
> - The narrator gets computed FACTS and the resolved scope — never the job of inferring either from a sample.
> - Two model calls per turn, low reasoning effort, verbatim figures.

---

## 7. Guardrails

Layered, structural, and each traceable to a failure it prevents.

**Tenancy** — `entity_id` comes from the session and is injected into every
query. In the SQL sandbox it is baked into the definition of the only two views
the model may name; the validator refuses any other table.

**Arithmetic** — done in SQL, including period-over-period deltas. The model's
prose is checked: any figure not present in the computed facts discards the
prose for a deterministic template (`narration = llm_rejected`, visible in the
trace).

**Entity resolution** — `MATCH` / `AMBIGUOUS` / `NOT_FOUND`. Unknown names are
reported honestly ("no transactions for Oracle, closest: OLA…"), ambiguous ones
become a question. The same resolver runs on names the model puts into SQL — as
parameters *or* inlined literals — so the fallback cannot turn "Oracle" into
"you spent nothing".

**Time** — an unparseable period is a clarification, never a silent widening.
Comparison baselines are derived (`previous_window`) so "the 3 months before
that" compares 3 months to 3 months, not 3 months to one. Windows are ordered
chronologically regardless of how the model filled the arguments.

**Direction** — always explicit for aggregates; mixing credits into "spend" is
the same class of error as summing failed payouts. Listings of "my latest
transaction" deliberately cover both directions so an incoming payment is never
hidden.

**The SQL sandbox** (`sqlguard.py`) — one `SELECT`, parsed by DuckDB before
execution; only `my_transactions` / `my_accounts`; no table functions
(`read_parquet`, …); literals as bound parameters with placeholder/parameter
count checked; `LIMIT 200` enforced; sensitive columns (`utr_number`, raw
`account_number`) simply absent from the views; `description` redacted in
results. Refusals are explained, never silently rerouted.

**PII** — account numbers masked to the last 4; long digit runs redacted before
any text reaches the model; the model never sees raw narration for generation.

**Disclosure over omission** — truncation ("showing 10 of 40 — the total covers
all"), exclusions ("excludes ₹46.9M of bank charges and self-transfers"),
unattributed share, and — when no model is configured — filters the rules
planner could not apply ("*'weekends'* needs the language model; this answer
does NOT apply it").

**Text that is not a name** — time phrases ("the 3 months before"), question
clauses ("which month I have high expense"), and day words ("on weekends") are
recognised and never resolved as counterparties.

> **In short**
> - Guardrails are structural: view scoping, parsed-before-run SQL, bound parameters, server-side identity.
> - The resolver sits on *every* path a name can take — typed tools and generated SQL alike.
> - Unknown periods and unknown names become questions or honest guardrails, never silent widening or "you spent nothing".
> - Disclosure beats omission: truncation, exclusions, coverage and un-appliable filters are all stated.

---

## 8. Handling ambiguous questions

Clarification is a **tool the graph can force**, not a prompt instruction. Five
gates trigger it; each stores the original question and partial arguments in
`pending` so the reply resumes the turn rather than starting a new one.

| Gate | Fires when | Example |
|---|---|---|
| Ambiguous counterparty | Resolver returns several close matches | "selection" → SELECTION ELECTRONICS / NAVYUG SELECTION / UMANG SELECTION |
| Unnamed person | "my friend / colleague / landlord" with no name | offers the people who have sent money, plus *Everyone* |
| Missing period on an aggregate | Counterparty has >1 month of history and no window given | "calculate my spending for swiggy" → 24 months of activity, which period? |
| Unparseable period | Token not in the grammar | "trailing month" → which period did you mean? |
| Model-initiated | The planner itself calls `ask_user` | rare; the gates usually get there first |

**What does *not* trigger a question** matters as much. A listing that is already
bounded — "last 5 transactions with BookMyShow", "highest transaction",
"transactions over 1 lakh", "latest transaction" — is answered directly; asking
"which period?" there is not clarification, it is a question the user already
answered. Explicit all-time wording ("Zomato **total**") forces `all_time` even
if the planner omitted it.

**Replies are interpreted generously.** "No", "all", "doesn't matter" mean *no
restriction*; "not only swiggy" also widens the counterparty; a bare "2026" is
a year; "Everyone" after the person question returns the combined total. A reply
the system cannot interpret re-asks with options rather than guessing.

**Comparison baselines are inferred, not asked.** "Compare it to the 3 months
before" derives the equal-length window immediately preceding the subject —
so the user is not asked to name a period that has no natural name.

> **In short**
> - Five deterministic gates decide when to ask; the model's judgement is not relied on.
> - Bounded listings and explicit all-time wording are answered directly — over-asking is a failure too.
> - Replies like "no", "all", "not only swiggy" and bare years resolve instead of looping.
> - The original question travels with the clarification so the resumed answer is narrated in context.

---

## 9. Observability

Every answer carries enough to be checked by a person who does not trust it.

**Confidence band** — High / Medium / Low, with an expandable *"How was this
confidence determined?"* listing each contributing reason. It scores
**interpretation risk**, not arithmetic (which is exact): resolver method
(exact 1.0 → fuzzy ≤0.92), whether the period was assumed (×0.92), unattributed
share of in-scope rows (×(1−share)), rejected narration (×0.85), and the SQL
fallback (×0.85). Factors multiply so mild doubts compound; bands are ≥0.88 /
≥0.72 / below. Bounded listings and explicit totals are *not* docked for "no
period". Full methodology: [ARCHITECTURE_V2.md §13](ARCHITECTURE_V2.md).

**Interpretation line** — "*Interpreted as counterparty SWIGGY, period May 2026,
money out*". A wrong window is visible, not silent.

**Carried-over context** — "*↩ Carried over from your previous question:
merchant = SWIGGY, period = last_month*" whenever inheritance fired.

**Notes** — truncation, exclusions, coverage, un-appliable filters, non-aligned
windows.

**Audit trace** — every node's record: the planner used (`llm` / `rules` /
`resume`) and the tool + arguments it chose; inheritance and normalisation
steps; counterparty resolution with method and score; which gate fired;
the resolved window and whether it was month-aligned; the exact SQL with bound
parameters shown, rows, source table and latency; for generated SQL, the query,
purpose and whether the counterparty guard intervened; the narration method
(`llm` / `llm_rejected` / `template`); the confidence score and reasons.

**Verifiable table + CSV** on every answer; result tables are stored (capped at
200 rows) so a conversation redraws after a restart without re-querying.

**Evaluation harness** — `eval_agent.py` runs 24 cases asserting terminal state
and resolved filters, on both planners, and **reports whether each run was
clean**: it records which planner every case actually used, so a rate-limited
"LLM" run that silently fell back to rules is flagged `clean? NO` rather than
scored. That detector caught two contaminated runs during development.

**Ingest reporting** — row counts, coverage by counterparty kind, top merchants,
anchor date, and the alias-map version, printed on every build.

> **In short**
> - Confidence is a band with itemised reasons; it scores interpretation, and the arithmetic is exact by construction.
> - Every answer states its resolved scope, what was inherited, and what was excluded or truncated.
> - The audit trace is the whole decision path — planner, resolution, gates, SQL, narration method, confidence.
> - The eval harness reports whether a run actually measured what it claims to.

---

## 10. Model choice & efficiency

**Configured now:** `gpt-4.1-mini` on OpenAI (via `.env`).
**Section 7 compliant option:** `openai/gpt-oss-20b` on Groq — open weights,
published size (~21B total, ~3.6B active per token). OpenAI publishes **no**
parameter counts, so no OpenAI API model can be *verified* as ≤20B; switching
back for judging is one `.env` line.

The model does two things per turn — pick a tool, narrate facts — and neither
touches data, which is why size matters less here than in a typical RAG build.

### Measured accuracy (24-case interpretation benchmark, `eval_agent.py`)

| Planner | Score | p50 | Clean run? |
|---|---|---|---|
| `gpt-4.1-mini` (OpenAI) | **24 / 24** | 2,513 ms | yes |
| Rules only (no API key) | **24 / 24** | 36 ms | yes |
| `gpt-4o-mini` (15-case set) | 14 / 15 | 2,228 ms | yes |
| `gpt-4.1-nano` (15-case set) | 13 / 15 | 1,660 ms | yes |
| `gpt-oss-20b` (Groq) | not yet clean — daily quota exhausted | — | no |

Nano is genuinely too weak: it failed the missing-period gate and misresolved a
named person. **Tool selection is the capability that varies with model size
here**, and it is not free.

### Efficiency

Two model calls per turn, ~3,200 tokens; `reasoning_effort="low"` where
supported (4× faster, measured); ~60 turns/day on Groq's free tier. The database
is ~1% of a turn. The honest gap remains: the compliant model has not had a
clean benchmark run; `python eval_agent.py --model …` settles it in one command
per candidate.

> **In short**
> - The model only routes and paraphrases; its size is a routing-quality question, not a correctness one.
> - `gpt-4.1-mini` and the no-model fallback both score 24/24; nano fails real cases.
> - Only `gpt-oss-20b` is *verifiably* ≤20B — switch back to it for judging with one `.env` line.
> - Two calls per turn is a deliberate latency and cost design.

---

## 11. Trade-offs

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| DB access | Typed tool calls, SQL fallback sandboxed | Text-to-SQL everywhere | Tenancy structural; injection surface closed; cost bounded |
| Orchestration | LangGraph DAG | ReAct loop | A loop cannot *guarantee* a clarification and costs 3–6 calls |
| Counterparty | Derived at ingest | Parsed per query | Enables `GROUP BY`; 70× faster; only way to rank vendors |
| Aggregates | Monthly rollup + fact fallback | Fact table only | 368k rows vs 4M; falls back rather than snapping windows |
| Clustering | On distinct strings | On rows | 3,000 vs 4,000,000 |
| Comparison baseline | Derived `previous_window` | Named tokens | "the 3 months before that" has no natural token |
| Confidence | Three bands + reasons | A percentage | Heuristics are not measurements; "93%" invites false precision |
| Graph state | Primitives + side-channel | Pickled rich objects | Pickle broke every turn on a hot-reload |
| Chat storage | Separate `chats.db` | Inside the warehouse | A data reload must not destroy conversations |
| UI | Streamlit | FastAPI + TLS | Speed to build; TLS at the UI is a known gap |

The real costs: typed tools limit expressiveness (mitigated by the sandbox);
the rollup cannot answer arbitrary windows (falls back); coverage is 94%, not
100% (disclosed); confidence weights are set by judgement, not fitted to data;
the SQL fallback lowers the band on purpose because no typed contract constrained
the mapping.

> **In short**
> - Reliability was chosen over expressiveness, then expressiveness recovered through a sandbox rather than by loosening the core.
> - Wherever a shortcut could produce a silently wrong answer (snapping windows, pickled state), the slower or stricter path was taken.
> - Confidence and coverage are reported as what they are — heuristics and a measurement — not dressed up.
> - The one architectural gap acknowledged rather than hidden is TLS termination at the UI.

---

## 12. Performance (measured, 4M rows)

| Operation | Store | Latency |
|---|---|---|
| Spend on a merchant, one month | rollup | **5.3 ms** |
| Spend all-time | rollup | **4.8 ms** |
| Spend over 30 days (not month-aligned) | fact | **8.9 ms** |
| Top counterparties | rollup | **8.6 ms** |
| List 50 transactions | fact | **11.2 ms** |
| Account balance | source | **1.8 ms** |
| Sandboxed SQL (count on weekends) | scoped view | ~10 ms |

Turn: ~2.5 s with `gpt-4.1-mini`, ~1.7 s with `gpt-oss-20b` on Groq when
measured, **36 ms** rules-only. Build: ~13 s for 4M rows; warehouse ~911 MB.

> **In short**
> - Every query shape is single-digit milliseconds at 4M rows; the model is the entire latency budget.
> - Rollup vs fact routing costs ~2× on non-aligned windows and nothing in correctness.
> - Build is seconds, not minutes, because parsing is vectorised SQL.
> - Numbers here are measured on this repository, not quoted from vendor pages.

---

## 13. Project layout

| File | Role |
|---|---|
| `app.py` | Streamlit chat UI, conversation list, audit drawers |
| `agent.py` | Tool schemas, planners, policy gates, context inheritance, confidence |
| `graph.py` | LangGraph state machine and per-turn side-channel |
| `queries.py` | Parameterised SQL; totals and truncation by contract |
| `sqlguard.py` | Sandbox for model-written SQL |
| `resolver.py` | Counterparty → MATCH / AMBIGUOUS / NOT_FOUND |
| `explainer.py` | Narration, templates, numeric verification, scope description |
| `llm.py` | Provider shim; learns per-model quirks |
| `enrichment.py` | Extraction, normalisation, rollups |
| `db.py` | Connections, time-range grammar, manifest |
| `datasource.py` | **Paste the organiser's URL here** |
| `ingest.py` | Live import + purge |
| `build_warehouse.py` | Full / incremental builds |
| `chatstore.py` | Conversations + checkpoints on disk |
| `session.py` | The logged-in customer (hardcoded) |
| `security.py` | Masking, redaction, UTR blind index |
| `config.py` | Schema map, aliases, model, paths |
| `test_warehouse.py` | 256 value-asserting tests |
| `eval_agent.py` | 24-case interpretation benchmark |

Documentation: [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md) (§12 data flows, §13
confidence, §14 agent-framework decision incl. §14.2.1 the SQL fallback) and
[BUGS.md](BUGS.md) (audited defects with reproductions).

> **In short**
> - Start with `datasource.py` (paste the URL), `ingest.py` (import) and `app.py` (run).
> - The agent is `agent.py` + `graph.py`; the guarantees live in `queries.py`, `sqlguard.py`, `resolver.py`, `explainer.py`.
> - `config.py` is the only place schema or provider changes are made.
> - `test_warehouse.py` and `eval_agent.py` are the evidence for every claim in this file.

---

## 14. Known gaps

- **Compliant model not cleanly benchmarked** — `gpt-oss-20b` on Groq hit the
  daily quota every time; one clean `eval_agent.py` run remains.
- **TLS terminates at the database, not the UI** — Streamlit cannot serve HTTPS;
  moving transport to FastAPI is the planned Phase 5.
- **UTR search is unavailable** without a decryption key; the blind index is
  implemented and idle.
- **Coverage measured on synthetic narration** — the ingest prints coverage so a
  drop on real data is visible immediately.
- **The rules planner cannot use the SQL fallback** and says so when a filter is
  beyond it.
- **No presentation deck or architecture image is generated yet.** The V1
  versions described the old vendor-payout schema and were removed rather than
  left to contradict this document; `legacy/` retains the V1 code only as the
  reproduction target for [BUGS.md](BUGS.md).

> **In short**
> - The highest-value open item is one clean benchmark run of the Section 7 compliant model.
> - The one architectural gap is HTTPS at the UI; data-side TLS is already in place.
> - Everything else here is a disclosed limitation, not a hidden one — coverage, UTR, the rules planner's reach.
> - A V2 presentation deck and architecture image still need to be produced; the V1 ones were removed.
