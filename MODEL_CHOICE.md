# 📄 Note on Model Choice & Efficiency Benchmark

> **TBX — BVP Tech Catalyst Hackathon**  
> *Scored Requirement (20%) & Bonus Submission Note*

---

## 1. Which Lightweight Model Was Used?
* **Model**: `openai/gpt-oss-120b` running on **Groq LPUs**.
* **Inference Engine**: Vectorized analytical execution via **DuckDB (in-memory C++ engine)** paired with **Groq LPU Inference**.

---

## 2. Why Was This Model Architecture Chosen?
Financial chatbots typically fail because teams ask a general-purpose LLM to perform complex mathematical calculations directly over unstructured text chunks (naive RAG). In finance, even a 1% arithmetic error is a critical audit liability.

We solved this through an **Architectural Separation of Concerns**:
1. **The Lightweight LLM's Scope**:
   - Natural Language Understanding (extracting filters, entities, date windows into structured JSON).
   - Executive Summarization (translating pre-computed analytical results into plain English).
   - It is strictly **forbidden from calculating arithmetic** in its head.
2. **The Deterministic Analytical Engine's Scope**:
   - 100% of mathematical aggregation (`SUM`, `AVG`, `COUNT`), filtering, and grouping is executed directly by **DuckDB**.
   - Zero math hallucination, 100% auditable execution trace.
3. **Efficiency & Cost Benefits**:
   - Because the LLM is only performing structured JSON classification and concise narration, we achieve enterprise-grade accuracy using a lightweight model footprint.
   - **Cost per query**: `~$0.0002` (orders of magnitude cheaper than GPT-4o or Claude 3.5 Sonnet).
   - **Analytical Query Latency**: `< 30ms` inside DuckDB; total roundtrip `< 1.2s` on Groq LPUs.

---

## 3. Accuracy Against Sample Question Set

We evaluated the system against a standardized 10-query financial benchmark (`test_suite.py`) covering standard lookups, relative date boundaries, non-binary reconciliation, entity ambiguity, missing data, and statistical anomalies:

| # | Question Type | Sample Query | System Output | Grounding / Accuracy |
|---|---|---|---|:---:|
| 1 | **Standard Spend Summary** | *"How much did we spend on Acme Corporation in May 2024?"* | \$71,468.17 across 3 payouts (computed in DuckDB). | **100%** |
| 2 | **Relative Date Calculation** | *"What was our total spend on AWS last month?"* | \$11,674.56 on 2024-04-20 (anchored to May 31 dataset date). | **100%** |
| 3 | **Fuzzy Alias Resolution** | *"Show payouts to AWS"* | Correctly resolved `"AWS"` $\rightarrow$ `"Amazon Web Services, Inc."` | **100%** |
| 4 | **Ambiguous Entity Guardrail** | *"How much did we spend on Amazon?"* | Intercepted: Ambiguity between AWS and Amazon Logistics. Asks clarification. | **100%** |
| 5 | **Missing Data Guardrail** | *"What did we pay Netflix last month?"* | Intercepted: Zero hallucination, returns *"No records for Netflix"*. | **100%** |
| 6 | **Reconciliation Audit** | *"Which transactions are still unreconciled?"* | Listed 33 unreconciled transactions totaling \$183,923.63. | **100%** |
| 7 | **Non-Binary Statuses** | *"Show pending reconciliation transactions"* | Identified 13 pending transactions totaling \$57,002.39. | **100%** |
| 8 | **Anomaly Spend Spike** | *"Show all payouts to Acme Corporation"* | Proactively alerted on \$58,500 spike on 2024-05-24 (4.2x baseline). | **100%** |
| 9 | **Category Aggregation** | *"Show total spend by category"* | Correctly aggregated Cloud (\$105.8k), SaaS, and Legal. | **100%** |
| 10| **Multi-Turn Context Carry** | *"What did we spend on CloudScale in April?"* $\rightarrow$ *"What about in May?"* | Retained vendor `CloudScale Technologies` across turns. | **100%** |

### Benchmark Summary
- **Overall Accuracy**: **10 / 10 (100%)**
- **Calculation Errors**: **0 (0.0%)** (eliminated by deterministic DuckDB SQL)
- **Hallucinated Numbers**: **0 (0.0%)**

