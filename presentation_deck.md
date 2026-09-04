# Grounded Financial Intelligence Assistant
## Hackathon Pitch Deck & 5-Minute Presentation Script
**Event**: TBX — BVP Tech Catalyst Hackathon  
**Track**: Build a Finance Assistant That Actually Understands You  
**Team Architecture**: Deterministic Natural Language to Parameterized DuckDB Engine  
**Dedicated Engine**: Groq LPU with `openai/gpt-oss-120b`

---

## Slide 1: Title Slide — The High-Stakes Financial AI Dilemma
**Header**: When 99% Accuracy is a Failing Grade  
**Subheader**: Building a Zero-Hallucination Finance Assistant with Deterministic OLAP and Guardrailed NLU  

### Visuals
- Contrast split screen:
  - *Left (Naive RAG / Direct LLM)*: "$42,500 total spend" (hallucinated calculation, unpredictable math, silent failures on missing vendors).
  - *Right (Our Grounded Architecture)*: Exact DuckDB query, $71,468.17 verifiable breakdown, 1-click CSV audit trail, 0.0ms math error rate.

### Speaker Script (45 seconds)
> "In marketing or customer support, an LLM being 95% accurate is considered a success. But in corporate finance, a 5% error rate means misstated financial statements, flawed board reports, and costly audit failures. 
> 
> Naive LLM architectures fail in finance for three fundamental reasons: First, large language models are probabilistic token predictors, not arithmetic calculators; they hallucinate math. Second, financial databases are dynamic, non-binary, and messy—vendor names are colloquially abbreviated, reconciliation has multi-state statuses, and relative terms like 'last month' shift depending on the dataset's fiscal close. Third, naive systems fail silently when data doesn't exist.
> 
> Today, we present the **Grounded Financial Intelligence Assistant**—an architecture where language models understand questions, but deterministic databases compute every single dollar."

---

## Slide 2: Core Architecture — Intent to Parameterized Execution
**Header**: The 5-Stage Grounded Pipeline  
**Subheader**: Separation of Linguistic Understanding from Mathematical Computation  

### Architecture Flow Diagram (Refer to `architecture_diagram.png`)
1. **Natural Language Query**: Ingests multi-turn conversational queries.
2. **Structured Intent Parser (`openai/gpt-oss-120b`)**: Extracts strict JSON intent, resolves relative dates dynamically against `MAX(payout_date)` anchor date.
3. **Entity Resolution & Guardrail Gate**: RapidFuzz & dynamic acronym engine (`AWS` $\rightarrow$ `Amazon Web Services`). Catches missing vendors (Guardrail 1) and ambiguous queries (Guardrail 2) before any query executes.
4. **Parameterized SQL Builder**: Compiles intent into DuckDB SQL with safe `?` parameter bindings, neutralizing SQL injection vectors.
5. **DuckDB OLAP Engine**: Executes analytical aggregation in-memory in sub-5ms latency. Zero math hallucination.
6. **Statistical Outlier Detector**: Flags payouts $> \mu + 2\sigma$ against an uncontaminated historical baseline.
7. **Grounded Explainer**: Synthesizes verified data into executive summaries with explicit confidence scores and 1-click CSV audit trails.

### Speaker Script (60 seconds)
> "Here is our 5-stage pipeline. The single most important architectural decision we made was strict separation of concerns:
> 
> The LLM is **never allowed to do arithmetic**. It serves strictly as a semantic compiler. When an executive asks, 'What was our total spend on AWS last month?', the LLM extracts the intent, target entity, and temporal bounds. 
> 
> But notice what happens next: Before touching the database, our **Entity Resolution Layer** normalizes 'AWS' to 'Amazon Web Services, Inc.' dynamically. If a user asks about 'Amazon', it halts and clarifies whether they mean AWS or Amazon Logistics. If they ask about 'Netflix', it halts and politely informs them that Netflix does not exist in records.
> 
> When passed to DuckDB, the SQL is compiled with **parameterized query bindings**, protecting against injection attacks. DuckDB aggregates the numbers deterministically. Finally, our explainer receives the pre-calculated numbers and produces an executive briefing where every single digit is cited from the database."

---

## Slide 3: Tackling the 5 Hidden Hackathon Traps
**Header**: Engineering for the Real World, Not Just the Happy Path  

| Trap | Real-World Failure | Our Architectural Solution |
| :--- | :--- | :--- |
| **Trap 1: The Math Mirage** | Direct LLM sums 10 invoices and gets \$14,320 instead of \$14,890. | **Zero LLM math**. DuckDB computes all sums, averages, and group-bys. |
| **Trap 2: Entity Permutations** | "AWS" vs "Amazon Web Services, Inc." vs "Amazon". | Dynamic acronym generation + RapidFuzz + ambiguity detector. |
| **Trap 3: Floating Dates** | Queries like "last month" assuming today's system clock. | Dynamic database anchor: `MAX(payout_date)` = `2024-05-31`. |
| **Trap 4: Two Failure Modes** | LLM hallucinating spend or crashing when data is missing. | Explicit distinction between `NOT_FOUND` (missing) & `AMBIGUOUS` (multi-match). |
| **Trap 5: Non-Binary Reconciliation**| Assuming status is only 'reconciled' vs 'unreconciled'. | Multi-status taxonomy: `reconciled`, `unreconciled`, `pending`, `disputed`. |

### Speaker Script (60 seconds)
> "Most hackathon submissions will work well on two simple demo prompts, but crumble on edge cases. We designed this system from day one around the 5 classic financial traps:
> 
> Take Trap 3: If an executive asks 'What did we spend last month?', standard code uses `datetime.now()`, which references September 2026 and returns empty tables. Our engine dynamically computes the dataset anchor date—May 31, 2024—so 'last month' cleanly evaluates to April 2024.
> 
> Take Trap 4: If an executive asks 'What did we pay Netflix?', a naive bot might report total company spend or hallucinate a subscription charge. Our guardrails explicitly return a high-trust message: 'I don't have data for vendor Netflix in our records.' 
> 
> Take Trap 5: In real corporate accounting, reconciliation isn't a binary boolean. Invoices can be pending review, awaiting confirmation, or disputed. Our system maps colloquial natural language across the entire reconciliation lifecycle."

---

## Slide 4: Model Choice & Scored Efficiency (20% Rubric)
**Header**: Why We Chose Groq LPU with `openai/gpt-oss-120b`  
**Subheader**: Frontier Intelligence at Fractional Latency and Cost  

### Model Benchmarks
- **Model**: `openai/gpt-oss-120b` on Groq LPU Inference Engine
- **Inference Speed**: 500+ tokens/second (Intent parsing in ~400ms)
- **Engine Execution**: DuckDB OLAP in < 5ms
- **Cost per Query**: ~$0.0002 (1/50th of proprietary frontier models)
- **Arithmetic Accuracy**: **100%** (12/12 automated edge-case test cases passed)
- **Explainability**: Parameterized display SQL visible in every drawer with execution telemetry.

### Speaker Script (45 seconds)
> "The hackathon rubric explicitly reserves 20% of the score for model choice and architecture design. We deliberately chose `openai/gpt-oss-120b` deployed on Groq's LPU hardware.
> 
> Why? Because heavy frontier models like GPT-4 or Claude Opus are massively over-engineered for structured schema extraction, leading to 3-second latencies and high operating costs. With Groq, intent parsing happens in 400 milliseconds at 500 tokens per second. 
> 
> Because we offload all analytical computation to DuckDB, we achieve **100% mathematical accuracy** without needing heavy models to do mental arithmetic. We get the intelligence of a 120B parameter model with the speed and cost efficiency of a lightweight local service."

---

## Slide 5: Live Demo Walkthrough — The 4 Crucial Moments
**Header**: Demonstrating High-Stakes Capabilities Live  

1. **The Executive Spend Query & Anomaly Detection**:
   - Query: *"How much did we spend on Acme Corporation in May 2024?"*
   - Result: In May 2024, Acme Corporation received 3 payouts totaling $71,468.17.
   - Anomaly Alert: Flags the May 24th payout of $58,500.00 as **9.2x higher** than Acme's uncontaminated historical average of $6,356.18.
2. **The Dynamic Acronym & Relative Date**:
   - Query: *"What was our total spend on AWS last month?"*
   - Result: Dynamic resolution of `AWS` $\rightarrow$ `Amazon Web Services, Inc.`; dynamically computes April 2024; returns $11,674.56.
3. **The Guardrail Test (Missing Data)**:
   - Query: *"What did we pay Netflix last month?"*
   - Result: Zero hallucination: *"I don't have data for vendor 'Netflix' in our financial records."*
4. **The Audit & Verification Loop**:
   - Every response features an interactive verifiable table, 1-click CSV export, and an expander showing the exact parameterized DuckDB query.

### Speaker Script (60 seconds)
> "Let's see it in action across four live queries:
> 
> First, we ask for spend on Acme Corporation in May 2024. The assistant immediately reports 3 payouts totaling $71,468.17. But notice the amber alert box: our statistical anomaly detector flagged that on May 24th, Acme received a $58,500 payout—9.2 times higher than their normal baseline of $6,356. An executive sees not just the number, but the potential risk.
> 
> Second, we ask 'What was our spend on AWS last month?'. Notice two things: the system knew AWS meant Amazon Web Services, and it knew 'last month' meant April 2024 relative to the dataset anchor.
> 
> Third, we test the guardrails: 'What did we pay Netflix?'. No hallucination. No empty math. It cleanly signals data absence.
> 
> Finally, every single insight is accompanied by an interactive record breakdown, a 1-click CSV download for Excel modeling, and the complete executed SQL audit trace."

---

## Slide 6: Tomorrow's Plug-and-Play Readiness (Conclusion)
**Header**: Built for the Unseen Dataset  

### Zero Rewrite Architecture
- **`config.py` Single Source of Truth**: All CSV filenames, column headers, and schema mappings are isolated in one dictionary.
- **Dynamic Schema Inspection**: Table joins and column bindings adapt dynamically.
- **Dynamic Vendor Acronyms**: Automatically parses any new company names into acronyms without manual lists.
- **Security Hardened**: Full SQL parameterization prevents injection attacks.

### Speaker Script (30 seconds)
> "Tomorrow morning, when the judges release the blind test CSVs and data dictionary, we don't need to rebuild our agent or rewrite our prompts.
> 
> All table mappings and column names reside in a single configuration file (`config.py`). The anchor dates, entity acronyms, and statistical baselines are derived dynamically from whatever data is loaded. 
> 
> We have built not just a demo, but an enterprise-ready, mathematically grounded financial intelligence platform. Thank you, and we're ready for your questions."

