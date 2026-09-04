# Sample Questions & Assistant Verification Log
> **TBX — BVP Tech Catalyst Hackathon**  
> *Submission Requirement: Sample questions and the corresponding answers produced by the assistant*

This document logs the exact 14 benchmark questions evaluated against the **Grounded Financial Intelligence Assistant**, demonstrating 100% mathematical accuracy, zero arithmetic hallucinations, robust guardrails, and statistical anomaly detection.

---

### 1. Standard Annual Spend Aggregation
* **Question**: `"How much did we spend on Salesforce in 2024?"`
* **Status**: `SUCCESS` | **Confidence**: `100% (High Certainty)`
* **Assistant Response**:
  > Salesforce.com Inc. received 6 payouts in 2024, totaling **$53,111.82** in spend. The average payout amount was **$8,851.97**, with the first payment recorded on 2024‑02‑16 and the most recent on 2024‑05‑21.
* **Underlying Records**: Interactive verifiable table displaying all 6 payout IDs, dates, descriptions, and amounts + 1-Click CSV export.
* **Executed SQL**:
  ```sql
  SELECT 
      v.vendor_name AS vendor_name,
      COUNT(p.payout_id) AS total_payouts,
      ROUND(SUM(p.amount), 2) AS total_spend,
      ROUND(AVG(p.amount), 2) AS average_payout,
      MIN(p.payout_date) AS earliest_payout,
      MAX(p.payout_date) AS latest_payout
  FROM vendor_payouts p
  JOIN vendors v ON p.vendor_id = v.vendor_id
  WHERE v.vendor_name = ? AND p.payout_date BETWEEN ? AND ?
  GROUP BY v.vendor_name;
  ```
  *(Bound parameters: `['Salesforce.com Inc.', '2024-01-01', '2024-12-31']`)*

---

### 2. Category Grouping & Ranking
* **Question**: `"Show total spend by category"`
* **Status**: `SUCCESS` | **Confidence**: `95% (High Certainty)`
* **Assistant Response**:
  > **Total spend by category**  
  > The highest spend is on **Cloud Infrastructure**, with 12 payouts totaling **$105,807.59**, followed by **Enterprise Software** at **$96,892.87** across 7 payouts and **Audit & Legal** at **$72,394.61** across 5 payouts. The lower‑spending categories include Observability ($11,042.69, 5 payouts) and Productivity ($11,646.00, 8 payouts), while the remaining categories fall between these ranges as shown in the table.
* **Executed SQL**:
  ```sql
  SELECT 
      v.category AS category,
      COUNT(p.payout_id) AS payout_count,
      ROUND(SUM(p.amount), 2) AS total_spend
  FROM vendor_payouts p
  JOIN vendors v ON p.vendor_id = v.vendor_id
  GROUP BY v.category
  ORDER BY total_spend DESC;
  ```

---

### 3. Chronological Payout Retrieval with Status Distinction
* **Question**: `"Show all payouts to Cloudflare Inc."`
* **Status**: `SUCCESS` | **Confidence**: `100% (High Certainty)`
* **Assistant Response**:
  > The most recent four payouts to Cloudflare Inc. are marked **Completed**: $1,551.24 on 2024‑05‑30, $1,932.80 on 2024‑05‑13, $1,792.17 on 2024‑04‑09, and $1,633.68 on 2024‑03‑16, each noted as “Payout to Cloudflare Inc. for monthly services.”  
  > Two payouts are still **Pending**: $1,687.64 on 2024‑03‑08 and $1,474.52 on 2024‑02‑08, also described as monthly‑service payouts.

---

### 4. Reconciliation Status Audit (Unreconciled State)
* **Question**: `"Which transactions are still unreconciled?"`
* **Status**: `SUCCESS` | **Confidence**: `95% (High Certainty)`
* **Assistant Response**:
  > The unreconciled transactions returned by the query are:
  > - $1,551.24 to Cloudflare Inc. on 2024‑05‑30 (payout for monthly services)
  > - $10,755.01 to WeWork Global on 2024‑05‑24 (payout for monthly services)
  > - $1,913.40 to CloudScale Technologies on 2024‑05‑23 (incidental expense)
  > - $12,173.83 to Amazon Web Services, Inc. on 2024‑05‑19 (payout for monthly services)
  > - $4,238.72 to Stripe Payments on 2024‑05‑19 (incidental expense)
  > - $1,856.34 to Amazon Logistics LLC on 2024‑05‑18 (incidental expense)
  > - $1,206.46 to Slack Technologies on 2024‑05‑17 (payout for monthly services)
  > - $2,958.37 to Stripe Payments on 2024‑05‑10 (incidental expense)
  > - $1,461.45 to Slack Technologies on 2024‑05‑03 (payout for monthly services)
  > - $6,803.90 to Google Cloud Platform on 2024‑04‑29 (payout for monthly services)  
  > All of these entries have a reconciliation status of **Unreconciled** with notes indicating they are **“Unmatched in ledger.”**

---

### 5. Dynamic Acronym + Relative Date Anchoring (Trap #2 & #3)
* **Question**: `"What was our total spend on AWS last month?"`
* **Status**: `SUCCESS` | **Confidence**: `98% (High Certainty)`
* **Dynamic Resolution**:
  - Entity: `AWS` $\rightarrow$ Dynamically resolved to `Amazon Web Services, Inc.`
  - Relative Date: Anchored to `MAX(payout_date) = 2024-05-31` $\rightarrow$ Evaluates to `2024-04-01` to `2024-04-30`.
* **Assistant Response**:
  > **Total spend on AWS last month: $11,674.56.** This amount reflects a single payout (1 transaction) made on April 20, 2024, which is both the earliest and latest payout date for the period. The average payout equals the total spend because there was only one transaction.

---

### 6. Quarterly Bound Calculation
* **Question**: `"How much did we spend on Google in Q1?"`
* **Status**: `SUCCESS` | **Confidence**: `98% (High Certainty)`
* **Dynamic Resolution**:
  - Entity: `Google` $\rightarrow$ `Google Cloud Platform`
  - Dates: `Q1` $\rightarrow$ `2024-01-01` through `2024-03-31`
* **Assistant Response**:
  > **Spend on Google Cloud Platform in Q1:**  
  > We made 4 payouts to Google Cloud Platform, totaling **$26,154.77**. The average payout was **$6,538.69**, with the first payment on 2024‑02‑04 and the most recent on 2024‑03‑27.

---

### 7. Non-Binary Reconciliation States (Trap #5: Pending Status)
* **Question**: `"Show pending reconciliation transactions"`
* **Status**: `SUCCESS` | **Confidence**: `95% (High Certainty)`
* **Assistant Response**:
  > **Pending Reconciliation Transactions (most recent 10)**  
  > - The latest pending entry is a $1,158.57 incidental expense for CloudScale Technologies dated May 28 2024.  
  > - The largest pending amount in this set is **$58,500.00** for the Acme Corporation enterprise license renewal on May 24 2024, followed by a $9,211.31 payout to Salesforce.com Inc. on May 21 2024.  
  > All listed transactions are marked **Pending** with the note “Awaiting bank confirmation,” covering a range of vendors and dates from mid‑February through late May 2024.

---

### 8. Non-Binary Reconciliation States (Disputed Status)
* **Question**: `"Are there any disputed transactions in our records?"`
* **Status**: `SUCCESS` | **Confidence**: `95% (High Certainty)`
* **Assistant Response**:
  > Yes, there are disputed transactions in the records. The most recent dispute is a **$13,420.50** charge on 2024‑05‑31 from **Amazon Web Services, Inc.** (AWS Monthly Compute charges for May) with a note of “Discrepancy in invoice total.” Earlier disputes include a **$4,180.78** expense on 2024‑04‑25 from **Acme Corporation** and a **$3,785.10** expense on 2024‑03‑07 from **Deloitte Advisory**, both also marked as “Disputed” with the same discrepancy note.

---

### 9. Missing Data Guardrail (Trap #4: Failure Mode 1)
* **Question**: `"What did we pay Netflix last month?"`
* **Status**: `NOT_FOUND` (Guardrail Triggered) | **Confidence**: `0%`
* **Assistant Response**:
  > I don't have data for vendor **'Netflix'** in our financial records. Please verify the vendor name.
* **Audit Trail**: `-- No SQL executed (Guardrail: Vendor not found)`  
  *(Zero math hallucination. Prevents inventing subscription fees).*

---

### 10. Dynamic Unknown Entity Extraction Guardrail
* **Question**: `"What did we spend on Snowflake this year?"`
* **Status**: `NOT_FOUND` (Guardrail Triggered) | **Confidence**: `0%`
* **Assistant Response**:
  > I don't have data for vendor **'Snowflake'** in our financial records. Please verify the vendor name.
* **Audit Trail**: `-- No SQL executed (Guardrail: Vendor not found)`

---

### 11. Ambiguous Entity Guardrail (Trap #4: Failure Mode 2)
* **Question**: `"How much did we spend on Amazon?"`
* **Status**: `AMBIGUOUS` (Guardrail Triggered) | **Confidence**: `50%`
* **Assistant Response**:
  > Your query is ambiguous as 'Amazon' matches multiple vendors in our system: **Amazon Web Services, Inc.**, **Amazon Logistics LLC**. Which vendor did you mean?
* **Confidence Signal**: `⚖️ Ambiguous Entity — Matches multiple vendors in database.`
* **Audit Trail**: `-- No SQL executed (Guardrail: Ambiguous entity)`

---

### 12. Statistical Anomaly Detection Spikes (Bonus Feature)
* **Question**: `"How much did we spend on Acme Corporation in May 2024?"`
* **Status**: `SUCCESS` | **Confidence**: `100% (High Certainty)`
* **Assistant Response**:
  > In May 2024, Acme Corporation received 3 payouts totaling **$71,468.17**, with an average payout of **$23,822.72**. The payouts occurred between 2024‑05‑10 (earliest) and 2024‑05‑24 (latest). Notably, the payout on 2024‑05‑24 of $58,500.00 is a significant spike—about 9.2 × higher than Acme’s historical average payout of $6,356.18.
* **Outlier Alert Banner**:
  > ⚠️ **Anomaly Alert**: Payout on **2024-05-24** of **$58,500.00** is **9.2x** higher than Acme Corporation's historical average of **$6,356.18** (std: $236.14).

---

### 13. Multi-Turn Conversational Memory Chain
* **Turn 1**: `"What did we spend on CloudScale in April?"`
  * **Response**: In April, we made a single payout to **CloudScale Technologies**, totaling **$4,329.80**. Since there was only one transaction, the average payout is also $4,329.80. Both the earliest and latest payout dates are the same: April 23, 2024.
* **Turn 2 (Follow-up without repeating vendor)**: `"What about in May"`
  * **Context Inherited**: Entity `"CloudScale Technologies"` automatically persisted from prior turn.
  * **Response**: In May, CloudScale Technologies recorded 1 payout. The total spend (and average payout) was **$3,815.37**, with the only payout occurring on May 9 2024.
* **Turn 3 (Pronoun follow-up)**: `"Show their individual payouts"`
  * **Pronoun Resolved**: *"their"* $\rightarrow$ CloudScale Technologies.
  * **Response**: Payout Details for CloudScale Technologies: On May 9 2024, a payout of $3,815.37 was made to CloudScale Technologies. The transaction is marked as Completed, with the description: “Payout to CloudScale Technologies for monthly services.”

---

### 14. SQL Injection Defense Verification
* **Question**: `"Show spend for category Cloud' OR '1'='1"`
* **Status**: `SUCCESS` | **Confidence**: `95%`
* **Assistant Response**:
  > I searched the financial dataset, but found no matching records.
* **Executed SQL**:
  ```sql
  SELECT 
      v.category AS category,
      COUNT(p.payout_id) AS payout_count,
      ROUND(SUM(p.amount), 2) AS total_spend
  FROM vendor_payouts p
  JOIN vendors v ON p.vendor_id = v.vendor_id
  WHERE LOWER(v.category) LIKE ?
  GROUP BY v.category
  ORDER BY total_spend DESC;
  ```
  *(Bound parameter: `["%cloud' or '1'='1%"]`)*  
  *Security verification: DuckDB safely treats the malicious string as a literal filter parameter; returns 0 rows without database dump or syntax error.*
