"""
Grounded Explanation Engine
===========================
Narrates the deterministic pre-computed DuckDB results in natural, executive-ready language.
Uses Groq with 'openai/gpt-oss-120b'.
The LLM never calculates numbers itself; it purely explains the facts returned by the database.
"""

import os
import pandas as pd
from groq import Groq
import config

def generate_explanation(user_query: str, query_info: dict, df: pd.DataFrame, anomalies: list = []) -> str:
    """Generates plain language explanation grounded strictly in pre-computed results."""
    
    # Empty result handling
    if df.empty:
        vendor = query_info.get("filters_applied", {}).get("vendor")
        start = query_info.get("start_date")
        end = query_info.get("end_date")
        
        date_str = f" between {start} and {end}" if start and end else ""
        vendor_str = f" for vendor '{vendor}'" if vendor else ""
        return f"I searched the financial dataset, but found no matching records{vendor_str}{date_str}."

    # Call Groq with openai/gpt-oss-120b
    groq_key = os.getenv("GROQ_API_KEY", config.GROQ_API_KEY)
    if groq_key:
        try:
            anomaly_context = "\n".join([f"- {a}" for a in anomalies]) if anomalies else "None"
            prompt = f"""You are an executive finance AI assistant.
Explain the following pre-computed query results answering the user's question.

User Question: "{user_query}"
SQL Executed:
{query_info.get('sql')}

Pre-Computed Data Table:
{df.head(10).to_dict(orient='records')}

Detected Statistical Anomalies:
{anomaly_context}

Formatting & Content Rules:
1. Explain the numbers clearly and professionally in 2-3 sentences.
2. DO NOT recalculate, aggregate, or guess any figures. Reference ONLY the exact numbers in the table.
3. If the user asked for the latest, most recent, or single payment, directly state the specific payout date, vendor name, amount ($X.XX), status, and description of that exact payment. DO NOT talk about total spend or average payouts.
4. If anomalies are listed above, mention the anomaly spike as a notable observation.
5. If the anomaly section says 'None', DO NOT mention anomalies or say 'no anomalies detected'.
6. Write clear plain markdown. Do not wrap currency amounts in LaTeX math syntax.
"""
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                model=config.ACTIVE_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            content = resp.choices[0].message.content.replace("\u202f", " ").replace("\xa0", " ")
            return content
        except Exception as e:
            print(f"Groq explainer notice, using fallback: {e}")

    # Fallback explanation if API call fails
    q_type = query_info.get("query_type")
    
    if q_type == "latest_payment":
        if not df.empty:
            row = df.iloc[0]
            v_name = row.get("vendor_name", "the vendor")
            p_date = row.get("payout_date", "")
            amt = float(row.get("amount", 0.0))
            status = row.get("status", "Completed")
            desc = row.get("description", "")
            desc_str = f" for '{desc}'" if desc else ""
            return f"The latest payment to **{v_name}** was **${amt:,.2f}** on **{p_date}** (Status: **{status}**){desc_str}."
        else:
            return "No payout records found matching your query."

    elif q_type == "spend_summary":
        if "total_spend" in df.columns:
            if len(df) == 1:
                row = df.iloc[0]
                v_name = row.get("vendor_name", "the vendor")
                total = float(row["total_spend"])
                count = int(row.get("total_payouts", 1))
                avg = float(row.get("average_payout", total / count))
                return f"Total spend for **{v_name}** was **${total:,.2f}** across **{count}** payout(s), with an average payout of **${avg:,.2f}**."
            else:
                top_v = df.iloc[0]["vendor_name"]
                top_spend = float(df.iloc[0]["total_spend"])
                total_all = float(df["total_spend"].sum())
                return f"Total spend across all {len(df)} vendors was **${total_all:,.2f}**. The highest spend was with **{top_v}** at **${top_spend:,.2f}**."

    elif q_type == "reconciliation_audit":
        unrec_count = len(df)
        total_unrec = float(df["amount"].sum()) if "amount" in df.columns else 0.0
        status_filter = query_info.get("filters_applied", {}).get("reconciliation_status", "unreconciled")
        return f"Found **{unrec_count}** transaction(s) with status **'{status_filter}'**, totaling **${total_unrec:,.2f}**."

    elif q_type == "category_summary":
        top_cat = df.iloc[0]["category"]
        top_amt = float(df.iloc[0]["total_spend"])
        return f"Top expense category is **{top_cat}** with **${top_amt:,.2f}** in payouts."

    else:
        row_count = len(df)
        total_amt = float(df["amount"].sum()) if "amount" in df.columns else 0.0
        return f"Retrieved **{row_count}** transaction record(s) matching your criteria, totaling **${total_amt:,.2f}**."
