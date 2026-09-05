"""
Structured Intent Parser
========================
Extracts strict, machine-readable JSON intent from free-form natural language queries.
Uses Groq with 'openai/gpt-oss-120b' for ultra-fast inference with dynamic entity extraction
and an intelligent, schema-aware fallback parser.
"""

import os
import re
import json
import calendar
from groq import Groq
import config

def get_system_prompt(anchor_date=None):
    ref_year = anchor_date.year if anchor_date else 2024
    ref_str = anchor_date.strftime('%Y-%m-%d') if anchor_date else "2024-05-31"
    
    return f"""You are a financial query intent parser for a company's internal finance database.
Given the user's natural language question and previous conversation history, extract the structured intent in strict JSON format.

CRITICAL REFERENCE DATE:
The dataset reference anchor date is {ref_str} (Year: {ref_year}).
ALL dates, months, and relative periods MUST be resolved relative to Year {ref_year}.
For example:
- "April" means start_date: "{ref_year}-04-01", end_date: "{ref_year}-04-30".
- "May" means start_date: "{ref_year}-05-01", end_date: "{ref_year}-05-31".
- "last month" should set date_filter type="relative", relative_value="last_month".

JSON Schema:
{{
  "intent": "spend_summary" | "transaction_list" | "latest_payment" | "reconciliation_audit" | "category_summary",
  "vendor_raw": "<extracted vendor name or null>",
  "limit": 1 | null,
  "date_filter": {{
    "type": "relative" | "absolute" | "all",
    "relative_value": "last_month" | "this_month" | "two_months_ago" | "last_quarter" | "ytd" | null,
    "start_date": "<YYYY-MM-DD or null>",
    "end_date": "<YYYY-MM-DD or null>"
  }},
  "reconciliation_status": "unreconciled" | "reconciled" | "pending" | "disputed" | "all",
  "category": "<extracted category or null>",
  "is_followup": true | false
}}

Rules:
1. If the user asks about spend grouped by category or department ("by category", "category spend", "show spend by category"), intent MUST be "category_summary".
2. If the user asks for the latest, most recent, or last single payment/payout ("latest payment", "latest single payment", "most recent payout", "last payment to X", "how much was the latest payment to X"), intent MUST be "latest_payment" and limit MUST be 1.
3. If the user asks about overall spend/cost/total paid over a time period ("how much did we spend in May", "total spend on AWS", "total paid to X"), intent is "spend_summary".
4. If the user asks for a list or history of multiple transactions/payouts ("show me transactions", "list payouts", "show all payouts"), intent is "transaction_list".
5. If the user asks about reconciliation, unreconciled items, status audits ("which transactions are unreconciled"), intent is "reconciliation_audit".
6. If the user asks about spend or payouts for a specific company or entity (e.g. "O'Brien Consulting", "Netflix", "Acme", "Salesforce"), you MUST extract that exact name into "vendor_raw". NEVER set "vendor_raw" to null when a specific entity name is mentioned!
7. If the user asks a follow-up ("what about the month before", "how about in May"), retain the previous vendor from conversation history and mark is_followup: true.
8. Return ONLY valid JSON.
"""

def parse_intent_llm(user_query: str, chat_history: list = [], anchor_date=None, known_vendors: list = None):
    """Uses Groq with openai/gpt-oss-120b to extract structured intent."""
    groq_key = os.getenv("GROQ_API_KEY", config.GROQ_API_KEY)
    if groq_key:
        try:
            client = Groq(api_key=groq_key)
            prompt = get_system_prompt(anchor_date)
            messages = [{"role": "system", "content": prompt}]
            for msg in chat_history[-4:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": user_query}) #add current question
            
            resp = client.chat.completions.create(
                model=config.ACTIVE_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0
            )
            parsed = json.loads(resp.choices[0].message.content)
            return parsed
        except Exception as e:
            print(f"Groq API notice, using fallback: {e}")

    # Fallback parser if API call encounters network issue or rate limit
    return parse_intent_fallback(user_query, chat_history, anchor_date=anchor_date, known_vendors=known_vendors)

def parse_intent_fallback(user_query: str, chat_history: list = [], anchor_date=None, known_vendors: list = None):
    """
    Dynamic rule-based backup parser that works with ANY dataset without hardcoded vendors.
    Correctly extracts unknown vendor names (e.g. "O'Brien Consulting") to trigger guardrails.
    """
    q = user_query.lower()
    ref_year = anchor_date.year if anchor_date else 2024
    
    # 1. Determine Intent
    intent = "spend_summary"
    limit = None
    if any(k in q for k in ["latest payment", "latest single payment", "most recent payment", "most recent payout", "last payment", "last payout", "single payment"]):
        intent = "latest_payment"
        limit = 1
    elif any(k in q for k in ["unreconciled", "reconciliation", "reconcile", "pending", "disputed"]):
        intent = "reconciliation_audit"
    elif any(k in q for k in ["category", "categories", "by department", "by category"]):
        intent = "category_summary"
    elif any(k in q for k in ["list", "show all", "show me", "which transactions", "history", "recent"]):
        intent = "transaction_list"
    elif any(k in q for k in ["how much", "total spent", "total spend", "what did we pay", "spend on"]):
        intent = "spend_summary"

    # 2. Reconciliation Status Filter
    recon_status = "all"
    if "unreconciled" in q:
        recon_status = "unreconciled"
    elif "reconciled" in q:
        recon_status = "reconciled"
    elif "pending" in q:
        recon_status = "pending"
    elif "disputed" in q:
        recon_status = "disputed"

    # 3. Date Filters (Dynamically anchored to dataset year)
    date_filter = {"type": "all", "relative_value": None, "start_date": None, "end_date": None}
    
    if any(k in q for k in ["last month", "previous month"]):
        date_filter = {"type": "relative", "relative_value": "last_month"}
    elif any(k in q for k in ["this month", "current month"]):
        date_filter = {"type": "relative", "relative_value": "this_month"}
    elif any(k in q for k in ["two months ago", "month before last", "month before"]):
        date_filter = {"type": "relative", "relative_value": "two_months_ago"}
    elif any(k in q for k in ["last quarter", "q1", "q2", "q3", "q4"]):
        match = re.search(r"\bq[1-4]\b", q) #/b means word boundary q[1-4] means q followed by one digit between 1 and 4
        q_val = match.group(0) if match else "last_quarter"
        date_filter = {"type": "relative", "relative_value": q_val}
    elif any(k in q for k in ["ytd", "year to date", "this year"]):
        date_filter = {"type": "relative", "relative_value": "ytd"}
    else:
        month_map = {
            "january": 1, "jan": 1, "february": 2, "feb": 2,
            "march": 3, "mar": 3, "april": 4, "apr": 4,
            "may": 5, "june": 6, "jun": 6, "july": 7,
            "august": 8, "aug": 8, "september": 9, "sep": 9,
            "october": 10, "oct": 10, "november": 11, "nov": 11,
            "december": 12, "dec": 12
        }
        for m_name, m_num in month_map.items():
            if re.search(rf"\b{m_name}\b", q):
                last_day = calendar.monthrange(ref_year, m_num)[1] #[1] gets the last day of the month i.e no of days 
                date_filter = {
                    "type": "absolute",
                    "start_date": f"{ref_year}-{m_num:02d}-01",
                    "end_date": f"{ref_year}-{m_num:02d}-{last_day:02d}"
                }
                break

    # 4. Dynamic Vendor Extraction (No hardcoded synthetic vendor list)
    is_followup = False
    vendor_raw = None

    # A. Search against real known_vendors if provided
    if known_vendors:
        for kv in known_vendors:
            clean_kv = kv.lower().replace(",", "").replace(".", "").replace("-", " ")
            if clean_kv in q:
                vendor_raw = kv
                break
            # Distinctive word check (e.g. "CloudScale", "Deloitte" )
            distinctive_words = [w for w in clean_kv.split() if len(w) > 4 and w not in ["technologies", "corporation", "platform", "services", "global", "limited", "company"]] #it will split amazon web services into [amzon,web,services] and it will filter out words which are less than 4 letters and also filter out common words like technologies, corporation, platform, services, global, limited, company we are removing these beacuse we like if someone like what services spend it may give wrong ans by hallucunating so thats why
            for dw in distinctive_words:
                if re.search(rf"\b{dw}\b", q):
                    vendor_raw = kv
                    break
            if vendor_raw:
                break

    # B. Pattern extraction for unknown entities (e.g. "What did we spend on O'Brien Consulting?")
    if not vendor_raw:
        entity_patterns = [
            r"(?:spend on|spent on|payouts? to|payments? to|paid to|transactions? for|records? for|cost of|fees for|with)\s+([A-Za-z0-9\x27&.\s\-]+?)(?:\s+(?:in|last|this|for|during|between|since|from|over|q[1-4]|ytd|yesterday)|[?.]|$)",
            r"(?:how much (?:did we pay|was paid to|went to))\s+([A-Za-z0-9\x27&.\s\-]+?)(?:\s+(?:in|last|this|for|during|between|since|from|over|q[1-4]|ytd|yesterday)|[?.]|$)"
        ]
        stopwords = {"all", "all vendors", "category", "everything", "total", "last month", "this month", "april", "may", "software", "infrastructure", "us", "them"}
        for pat in entity_patterns:
            m = re.search(pat, user_query, re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                if cand.lower() not in stopwords and len(cand) > 2:
                    vendor_raw = cand
                    break

    # C. Multi-turn context check
    if not vendor_raw and chat_history:
        for prev in reversed(chat_history):
            if prev.get("role") == "user":
                prev_text = prev.get("content", "").strip()
                if known_vendors:
                    for kv in known_vendors:
                        if kv.lower() in prev_text.lower():
                            vendor_raw = kv
                            is_followup = True
                            break
                if vendor_raw:
                    break

    return {
        "intent": intent,
        "vendor_raw": vendor_raw,
        "limit": limit,
        "date_filter": date_filter,
        "reconciliation_status": recon_status,
        "category": None,
        "is_followup": is_followup
    }
