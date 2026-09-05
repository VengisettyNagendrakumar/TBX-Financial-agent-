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
      Apr–Jun 2026: ₹306,936.70 vs Jan–Mar 2026: ₹437,823.80
      — down ₹130,887.10 (29.9%)

You   I want to calculate my spending for swiggy
      I have 24 months of activity for SWIGGY (2024-07 to 2026-06).
      Which period would you like?
      [Last month] [Last 3 months] [Last 6 months] [This year] [All time]

You   What did I spend on Oracle?
      I have no transactions for Oracle. The closest names on record are
      OLA, KARAN MALHOTRA, RELIANCE DIGITAL.
```

---

## 1. Setup

**Requires Python 3.10+** (developed on 3.14).

```bash
pip install -r requirements.txt
```

Optional — for model-written phrasing rather than templates. The app is fully
functional without it:

```bash
echo GROQ_API_KEY=your_key_here > .env
```

### Run with demo data

```bash
python data_generator.py --rows 4000000     # ~12s; use --rows 200000 to go faster
python build_warehouse.py                   # ~13s at 4M rows
streamlit run app.py
```

`data_generator.py` produces synthetic Indian bank narration in the formats from
the provided sample (UPI / NEFT / IMPS / FT / RTGS), deliberately including
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

Run `--check` first. It verifies the connection and every column we read
*without deleting anything* — discovering a renamed column after wiping the demo
data, live, is a bad minute. If a table or column differs, it names the file to
edit (`datasource.TABLE_OVERRIDES` or `config.SCHEMA_CONFIG`).

`--purge` matters: without it an import **adds to** the demo data and answers
would mix real and synthetic transactions — worse than an obvious failure,
because it looks like it worked.

| Flag | Use |
|---|---|
| `--url "mysql://…"` | URL on the command line instead of the file |
| `--limit 100000` | Smoke-test the pipeline on a slice first |
| `--incremental` | Top up an existing warehouse |
| `--allow-insecure` | Only if their endpoint has no TLS (warns every time) |
| `--purge-only` | Wipe the demo data and stop |
| `--purge-chats` | Also delete saved conversations |

Supports `mysql`, `postgresql`, `sqlite`, `duckdb`, and folders of parquet/CSV.
Rows stream through DuckDB's native extensions, never through Python memory.

### Tests

```bash
python test_warehouse.py      # 193 assertions
```

These assert **values, not shapes**. Every numeric expectation is computed
independently from the source data and compared against what the warehouse
reports, so a regression in aggregation fails rather than passing green — the
V1 suite claimed "13/13, 0% math error" while only checking `len(table) > 0`
([BUGS.md](BUGS.md) B10).

---

## 2. Architecture

### The problem that shapes everything

The schema has **three tables — `bank`, `account`, `transaction` — and no vendor
table.** "Swiggy" exists only inside free-text narration:

```
UPI-NAVYUG SELECTION-XXXXXX8672-AUBL0002125-103293775381-260514201735136
NEFT  - UTIB0002678 - 95604250 - 915020031685136 - UMANG SELECTION
IMPS/P2A/600228462725/UTIB/918020101986700/00/INET/9211/SELECTIONMALIGAI/…
FT -  95842568 -  50200013729069 - SELECTION ELECTRONICS   DAHISAR EAST
```

Two consequences: **"which vendor did I spend the most on?" is impossible
without a grouping key** (you cannot `GROUP BY` a substring you never
extracted), and **`LIKE '%swiggy%'` cannot be indexed** — a leading wildcard
scans every description on every query.

So the counterparty dimension is **derived once at ingest**, not parsed per
query. That single decision drives the data layer.

### Pipeline

```
  MySQL / Postgres / files                     ← organiser's DB, TLS
            │  DuckDB native extension, streaming
            ▼
  ①  land        raw_bank / raw_account / raw_transaction
            ▼
  ②  extract     narration → counterparty            (SQL, vectorised)
            ▼
  ③  normalise   3,000 distinct strings → canonical  (Python, on the vocabulary)
            ▼
  ④  map + sort  txn_fact, clustered by (entity, merchant, date)
            ▼
  ⑤  aggregate   rollup_monthly + merchant_dim
```

**Step ③ is the load-bearing trick.** 4M transactions contain only ~3,000
distinct merchant strings — one per 1,333 rows. Fuzzy-clustering 3,000 strings
is trivial; clustering 4M is impossible. **Normalise the vocabulary, not the
rows.**

Extraction is generic rather than per-rail: split on the dominant delimiter,
keep the most name-like field (purely alphabetic, 3+ chars, not a rail keyword
or masked account). Fixed field positions drift between banks — in the sample,
IMPS/P2A puts the name at field 9 and IMPS OW at field 3.

### Three stores, three jobs

| Store | Holds | Answers | Size at 4M |
|---|---|---|---|
| `merchant_dim` | the **vocabulary** | "did you mean Swiggy?" | 11,000 |
| `rollup_monthly` | the **answers** | "how much on Swiggy in May" | 368,136 |
| `txn_fact` | the **evidence** | "show me those 28" | 4,000,000 |

The router uses the rollup only when a window is **provably month-aligned**;
anything else falls back to the fact table. Silently snapping "last 30 days" to
month boundaries would be a wrong answer, and the fallback is only ~2× slower.

### The agent

A **LangGraph state machine** ([graph.py](graph.py)) over a closed set of typed
tools. The model contributes **exactly one decision per turn** — which tool,
with which arguments — and every edge after that is code.

```
plan ─▶ inherit ─┬─▶ ask_user ──────────────────────────▶ CLARIFY
                 ├─▶ balances ──────────────────────────▶ ANSWER
                 └─▶ resolve_entity ─┬─────────────────▶ CLARIFY / GUARDRAIL
                                     ▼
                               gate_person ────────────▶ CLARIFY
                                     │
              ┌──────────────────────┴──────┐
              ▼                             ▼
          compare ─▶ ANSWER        resolve_period ─▶ CLARIFY
                                            │
                                            ▼
                                        execute ─▶ narrate ─▶ ANSWER
```

**Five of nine nodes can end the turn.** That is why it is a graph: the
guardrails *are* the control flow.

| Exit | Trigger |
|---|---|
| `CLARIFY` | counterparty matches several names |
| `CLARIFY` | "my friend" — one unnamed person |
| `CLARIFY` | period given but unparseable |
| `CLARIFY` | no period, counterparty spans months |
| `GUARDRAIL` | counterparty absent from history |

Clarification is a **policy gate in code**, not a prompt instruction. Prompting
a model to "ask when unsure" is a hope; a gate is a guarantee. Real evidence:
while testing, the planner chose `get_spend` on one turn and
`rank_counterparties` on another for the same "my friend" question — both were
caught because the gate runs outside the model's control.

### How answers stay grounded

1. **The model never writes SQL.** It fills typed arguments; queries are
   parameterised and built in code.
2. **The model never does arithmetic.** Every total, average and delta —
   including period-over-period differences — is computed in SQL.
3. **`entity_id` is injected server-side.** The model can choose *what* to
   filter, never *whose data* to read.
4. **Numeric verification.** Every figure in the model's prose is checked
   against the computed facts; an ungrounded number causes the whole narration
   to be discarded for a deterministic template.
5. **Truncation and exclusions are disclosed**, never silent: "showing 10 of 40
   — the total covers all of them".

---

## 3. Model choice & efficiency

*This section is the Section 7 deliverable: which model, why, and what accuracy
looked like against a sample question set.*

### 3.1 Selected model

**`openai/gpt-oss-20b` on Groq**, set in [config.py](config.py) and overridable
with `GROQ_MODEL`.

The LLM does exactly two things per turn, and neither involves data:

| Call | Job | Explicitly not its job |
|---|---|---|
| 1 | Pick a tool and fill typed arguments | Writing SQL |
| 2 | Narrate pre-computed facts | Any arithmetic |

Everything between those two calls is code: entity resolution, guardrails,
period maths, SQL, aggregation, and the delta in a period-over-period
comparison. This is why model *size* matters less here than in a typical RAG
build — a smaller model has strictly less opportunity to be wrong, because it
is never the thing computing an answer.

### 3.2 Section 7 compliance

The cap is 20B parameters. `gpt-oss-20b` is mixture-of-experts: **~21B total,
~3.6B active per token.** The active count is comfortably inside the ceiling;
the total is marginally above it. Stated plainly rather than rounded in our
favour — a judge checking the parameter count should hear it from us first.

Database scale is well inside the 20M-record limit: measured on **4M rows**
end to end (§5).

### 3.3 Accuracy against the question set

[eval_agent.py](eval_agent.py) runs 15 cases covering every behaviour the brief
names, plus the failure modes that matter in finance:

```bash
python eval_agent.py                  # both planners
python eval_agent.py --planner rules  # no API key needed
```

It scores **interpretation**, not arithmetic. Arithmetic cannot fail in a way a
benchmark would catch — totals come from SQL, and `test_warehouse.py` already
asserts them against independently computed values. What varies is whether a
question was *understood*, so each case asserts the terminal state and the
resolved filters (counterparty, window, direction), never the wording.

| # | Case | Expected |
|---|---|---|
| 1 | Merchant spend, explicit month | `SWIGGY`, 2026-05-01 → 05-31 |
| 2 | All-time total | `ZOMATO`, no window |
| 3 | Ranking counterparties | ranking, no counterparty filter |
| 4 | "my friend" — unnamed person | **CLARIFY** with candidates |
| 5 | Counterparty with no period | **CLARIFY** which period |
| 6 | Unknown counterparty | **GUARDRAIL**, no invented figure |
| 7 | Ambiguous counterparty | **CLARIFY** with candidates |
| 8 | Typo (`swigy`) | resolves to `SWIGGY` |
| 9 | Legal name (`BUNDL TECHNOLOGIES`) | folds to `SWIGGY` |
| 10 | Follow-up ("show me these") | inherits merchant **and** window |
| 11 | General question after a merchant turn | scope resets |
| 12 | "the 3 months before that" | baseline derived, both 3 months |
| 13 | "my balance" | one account, not ten |
| 14 | Credits from a named person | direction = credit |
| 15 | Time phrase as counterparty | not resolved as a vendor |

**Measured result**

| Planner | Score | p50 latency | Clean run? |
|---|---|---|---|
| Rules only (no API key) | **15 / 15 (100%)** | **31 ms** | yes |
| LLM (`gpt-oss-20b`) | not measurable | — | **no** |

The LLM row is **not reported**, and that is deliberate. The Groq daily
allowance (200k tokens) was exhausted during development, so every case fell
back to the rules planner mid-run. The harness detects this — it records which
planner each case actually used and prints `clean? NO (0/15)` — because a run
that silently degrades would otherwise score 15/15 while measuring nothing.
Re-run `python eval_agent.py` once quota resets to obtain the figure.

Each behaviour above *was* verified individually against the LLM planner during
development; what is missing is a single clean end-to-end run, not the
capability.

### 3.4 Efficiency

| Metric | Measured |
|---|---|
| Model calls per turn | **2** (plan, narrate) |
| Tokens per planning call | ~1,600 |
| Tokens per turn | ~3,200 |
| Turns per day on Groq's free tier | ~60 (200k token/day cap) |
| Turn latency, LLM planner | ~1.7 s |
| Turn latency, rules planner | ~11 ms |
| Database share of a turn | ~1% (1.8–11.2 ms) |

`reasoning_effort="low"` is set on both calls — **4× faster** than the default
(969 ms vs 3,965 ms on a trivial prompt) with no quality loss for tool selection
or summarisation.

Two calls per turn is a design constraint, not an accident: it is the main
reason a ReAct loop was rejected (§4), since a loop's 3–6 calls would put turn
latency past 3 s and roughly halve the number of demo questions the free tier
allows.

### 3.5 The honest gap

Section 7 says *"the smallest model that can still deliver accurate answers…
defaulting to the largest available will be scored down."*

**We currently ship the largest model the rules allow, without a comparative
benchmark.** That is compliance, not the optimisation being scored. Given §3.1 —
the model only selects a tool and paraphrases numbers it is handed — an 8B model
plausibly ties.

The harness already accepts a model override, so settling this is one command
per candidate:

```bash
python eval_agent.py --model llama-3.1-8b-instant
python eval_agent.py --model gemma2-9b-it
python eval_agent.py --model openai/gpt-oss-20b
```

Ship the smallest that scores 15/15. Tracked as [BUGS.md](BUGS.md) B16; it is
the highest-value work remaining, and it would likely fix the latency gap in §5
at the same time.

### 3.6 Running without a model

No API key is required. A deterministic rule-based planner maps questions onto
the same tools using the merchant vocabulary in the warehouse, and template
narration produces correct — if plainer — answers. It scores **15/15** on the
same question set at **31 ms**.

The failure mode is therefore degraded phrasing, not a broken assistant. A rate
limit mid-demo costs you fluency, not answers — which is exactly what happened
during development, and the app kept working.

---

## 4. Trade-offs

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| DB access | Typed tool calls | Text-to-SQL | Tenancy scoping becomes structural, not prompted; query cost is bounded; the surface is finite and testable |
| Orchestration | LangGraph DAG | ReAct loop | A loop cannot *guarantee* a clarification, and costs 3–6 model calls instead of 2 |
| Counterparty | Derived at ingest | Parsed per query | Enables `GROUP BY`; 70× faster; the only way to answer "which vendor most" |
| Aggregates | Monthly rollup + fact fallback | Fact table only | 368k rows vs 4M; falls back rather than snapping windows |
| Merchant clustering | On distinct strings | On rows | 3,000 vs 4,000,000 — the difference between feasible and not |
| Comparison baseline | Derived (`previous_window`) | Named period tokens | "the 3 months before that" has no natural token; `previous_3_months` reads as a synonym of `last_3_months` |
| Confidence | Three bands | A percentage | A combination of heuristics is not a measurement; "93%" invites false precision |
| UI | Streamlit | FastAPI + TLS | Faster to build; TLS termination is Phase 5 |
| Chat storage | Separate `chats.db` | Inside the warehouse | A data reload must not destroy conversations |

### The ones with real costs

**Tool calls limit expressiveness.** We answer only what a tool exists for. In a
domain where a wrong number is a liability, eight reliable answers beat thirty
unreliable ones — and the brief explicitly scopes to a subset.

**The rollup cannot answer arbitrary windows.** It is month-grained. The router
is deliberately conservative: rollup only when provably month-aligned, fact
table otherwise (~2× slower, still single-digit ms).

**Coverage is 94%, not 100%.** The remaining 6% is genuinely opaque narration
(`TRF/27964914/15335598`). It is reported as unattributed and lowers the
confidence band rather than being silently dropped — an answer that says "₹4.2L
identified, ₹31k unattributed" is honest; one that quietly omits 6% is not.

**Confidence bands are calibrated by judgement, not fitted to data.** There is
no labelled set of correctly-vs-incorrectly-interpreted answers to fit against.
The bands are ordinal: "High" does not assert a 90% success rate.

**Checkpoints use a pickle fallback.** Turn state carries a DataFrame between
nodes, which msgpack cannot encode (~9 KB/turn). The clean fix — keeping rich
objects out of graph state — restructures every node and is noted as a cleanup.

**The logged-in user is hardcoded** in [session.py](session.py). Auth is out of
scope per the brief, but data scoping is still required for correctness.

---

## 5. Performance (measured, 4M rows)

Query layer, scoped to one customer with 10 accounts:

| Operation | Store | Latency |
|---|---|---|
| Spend on a merchant, one month | rollup | **5.3 ms** |
| Spend all-time | rollup | **4.8 ms** |
| Spend over 30 days (not month-aligned) | fact | **8.9 ms** |
| Top counterparties | rollup | **8.6 ms** |
| List 50 transactions | fact | **11.2 ms** |
| Account balance | source | **1.8 ms** |

End-to-end turn: **~1.7 s** with the LLM planner, **~11 ms** rules-only.

The database is ~1% of a turn; **the model is the entire budget.** That is why
the number of model calls is the latency design, and why a ReAct loop was
rejected. The plan targeted <1 s; measured Groq round trips are ~800 ms each
rather than the ~300 ms assumed, so two calls land at ~1.7 s. Dropping to an
8B model (§3) is the most direct route back under the target.

Build: **~13 s** for 4M rows (land 1.9s, extract 6.0s, normalise 2.2s, map+sort
6.1s, aggregate 0.9s). Warehouse ~911 MB.

---

## 6. Project layout

| File | Role |
|---|---|
| `app.py` | Streamlit chat UI, conversation list, audit drawers |
| `agent.py` | Tool schemas, planners, policy gates, confidence |
| `graph.py` | LangGraph state machine — the turn as nodes and edges |
| `queries.py` | Parameterised SQL; totals and truncation by contract |
| `resolver.py` | Counterparty resolution → MATCH / AMBIGUOUS / NOT_FOUND |
| `explainer.py` | Narration + numeric verification |
| `enrichment.py` | Extraction, normalisation, rollups |
| `db.py` | Connections, time-range grammar, manifest |
| `datasource.py` | **Paste the organiser's URL here** |
| `ingest.py` | Live import + purge |
| `chatstore.py` | Conversations and LangGraph checkpoints on disk |
| `session.py` | The logged-in customer (hardcoded) |
| `security.py` | Masking, PII redaction, UTR blind index |
| `config.py` | Schema map, aliases, model, paths |
| `test_warehouse.py` | 193 value-asserting tests |
| `eval_agent.py` | Interpretation accuracy on the question set (§3.3) |

**Documentation**

- [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md) — full design. §12 data flows,
  §13 confidence methodology, §14 agent-framework comparison
- [BUGS.md](BUGS.md) — audited defects with reproductions

---

## 7. Known gaps

- **Smaller-model benchmark not run** (§3) — the largest permitted model ships
  unjustified. Highest-value remaining work.
- **TLS terminates at the DB, not the UI.** `ingest.py` connects over TLS;
  Streamlit cannot serve HTTPS itself. Phase 5 moves transport to FastAPI.
- **UTR search is unavailable** without a decryption key. The column arrives as
  ciphertext; a blind index is implemented but needs the key. A bare "reference
  number" question hits the plaintext `transaction_reference_id` instead.
- **Coverage measured on synthetic narration.** Real exports will differ; the
  ingest reports coverage so a drop is visible immediately.
- **`sample_qa.md`, `presentation_deck.md`, `build_deck.py`,
  `generate_diagram.py` and `test.py` are V1 artefacts** describing the old
  vendor-payout schema. They are stale and should be regenerated or removed
  before submission. The two scripts also need `matplotlib` and `python-pptx`,
  which are deliberately **not** in `requirements.txt` — nothing in the running
  app imports them.
