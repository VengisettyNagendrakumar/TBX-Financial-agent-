# 💼 Grounded Financial Assistant

> **TBX — BVP Tech Catalyst Hackathon**  
> *Problem Statement: Build a Finance Assistant That Actually Understands You*

An auditable, sub-second conversational financial intelligence assistant. Designed specifically for high-stakes finance operations: **100% mathematical grounding**, **zero arithmetic hallucinations**, and a **lightweight model footprint**.

---

## 🏗️ Architecture Overview

Naive RAG (vector chunk retrieval) fails in corporate finance because embeddings cannot calculate exact mathematical sums or enforce rigid date windows. 

Instead, our assistant decouples **Natural Language Understanding** from **Mathematical Execution**:

![Grounded Financial Intelligence Architecture](architecture_diagram.png)

```
                              User Question
                     ("How much for Acme in May?")
                                  │
                                  ▼
                   ┌──────────────────────────────┐
                   │    Structured Intent Parser  │
                   │   (Lightweight LLM / Regex)  │
                   └──────────────┬───────────────┘
                                  │ JSON Intent
                                  ▼
                   ┌──────────────────────────────┐
                   │    Entity & Date Resolver    │
                   │  • 3-Way Fuzzy Matching      │
                   │  • Dynamic Max-Date Anchor   │
                   └──────────────┬───────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │                                 │
           [Safe Match]                   [Guardrails Triggered]
                 │                                 │
                 ▼                                 ▼
   ┌───────────────────────────┐     ┌───────────────────────────┐
   │ Deterministic SQL Builder │     │  • Ambiguous: "Did you    │
   │  (Parameterized Python)   │     │    mean AWS or Logistics?"│
   └─────────────┬─────────────┘     │  • Missing: "No data on X"│
                 │ Valid SQL         └───────────────────────────┘
                 ▼
   ┌───────────────────────────┐
   │  In-Memory DuckDB Engine  │  <-- 100% Exact Math (SUM, AVG)
   │  Sub-15ms Analytical Math │
   └─────────────┬─────────────┘
                 │ Pre-Computed Data + Rows
                 ▼
   ┌───────────────────────────┐
   │  Statistical Anomaly Node │  <-- Bonus: Flags spend > 2σ
   └─────────────┬─────────────┘
                 │
                 ▼
   ┌───────────────────────────┐
   │ Grounded Explainer (LLM)  │  <-- Explains computed facts only
   └─────────────┬─────────────┘
                 │
                 ▼
   ┌────────────────────────────────────────────────────────┐
   │ Interactive UI (Streamlit)                             │
   │ • Plain Language Explanation                           │
   │ • Verifiable Underlying Records Table                  │
   │ • 1-Click CSV Export (Good to Have)                    │
   │ • Audit Trace: Full SQL & Sub-30ms Latency Metrics     │
   └────────────────────────────────────────────────────────┘
```

---

## 🎯 How This System Wins on the Rubric

| Criterion | Weight | How Our Architecture Delivers |
|---|---|---|
| **Accuracy & Grounding** | **30%** | Calculations occur inside DuckDB (`SUM`, `AVG`, `COUNT`). The LLM never computes arithmetic in its head, guaranteeing 0% calculation hallucinations. |
| **Model Efficiency** | **20%** | Uses Groq LPU engine with `openai/gpt-oss-120b`. Average inference is **500+ tok/s** with deterministic DuckDB OLAP compute in **`< 5ms`** and total cost **`~ $0.0002 / query`**. |
| **Natural Language Understanding** | **15%** | Dynamic acronym resolution (AWS, GCP), relative dates ("last month", "Q1", "YTD") anchored dynamically to `MAX(payout_date) = 2024-05-31`, non-binary reconciliation statuses, and multi-turn context memory. |
| **Functionality** | **15%** | Parameterized `?` query execution (neutralizing SQL injection), verifiable breakdowns, multi-turn memory, and 1-click CSV export. |
| **User Experience** | **10%** | Clean dark-mode executive UI, live demo quick-prompt buttons, KaTeX currency escaping, and audit trail drawers. |
| **Bonus Features** | **Bonus** | 1. **Statistical Anomaly Alerts** ($> 2\sigma$ spend spike detection against uncontaminated historical baselines).<br>2. **Confidence Signalling** (High / Moderate / Low).<br>3. **Export to CSV** on every verifiable record. |

---

## ⚡ Quick Start

### 1. Prerequisites
Python 3.10+ installed.

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Generate Mock Dataset (Digital Twin)
```bash
python data_generator.py
```

### 4. Run Automated Test Suite (12/12 Edge Cases)
```bash
python test_suite.py
```

### 5. Launch Interactive Web UI
```bash
streamlit run app.py
```

---

## 🔄 Swapping Real Hackathon Data Tomorrow Morning (5 Minutes)

When the hackathon organizers provide the official starter dataset:
1. **Drop the CSVs** into the `data/` directory.
2. **Open `config.py`** and update the column names in `SCHEMA_CONFIG` to match the organizers' data dictionary:
   ```python
   SCHEMA_CONFIG = {
       "vendors": {"file": "vendor_list.csv", "id_col": "vendor_id", "name_col": "vendor_name", ...},
       "vendor_payouts": {"file": "vendor_payouts.csv", "date_col": "payout_date", "amount_col": "amount", ...}
   }
   ```
3. **Run `python test_suite.py`** to confirm all queries execute. Done!

---

## 📊 Model Choice Rationale (For Presentation Deck)

- **Selected Model**: `openai/gpt-oss-120b` (via Groq LPU Inference Engine).
- **Rationale**: 
  - Scoped exclusively to **structured semantic entity parsing** and **executive summarization**.
  - All mathematical aggregations (`SUM`, `AVG`, `COUNT`), filters, and anomaly baselines are offloaded to **DuckDB (C++ vectorized analytical SQL)** with safe `?` parameter bindings.
  - Achieves **100% mathematical accuracy**, eliminates math hallucinations, and delivers sub-second end-to-end responsiveness.

---

## 📁 Key Submission Deliverables

- `app.py`: Streamlit conversational interface with verifiable dataframes, CSV downloads, and execution audit trace.
- `architecture_diagram.png`: Visual architecture schematic illustrating the 5-stage pipeline.
- `presentation_deck.md`: Slide-by-slide 5-minute presentation script with speaker notes and rubric mappings.
- `sample_qa.md`: Question and answer benchmark across the 5 core financial traps.
- `test_suite.py`: 12 automated edge-case unit tests.

