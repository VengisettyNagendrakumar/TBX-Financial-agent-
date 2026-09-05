"""
End-to-End Financial Pipeline Orchestrator
==========================================
Coordinates Intent Parsing -> Entity Resolution -> Guardrail Checks ->
Deterministic Query Building -> DuckDB Execution -> Anomaly Detection ->
Grounded Explanation.
"""

import time
import pandas as pd
from db import get_db_connection, get_anchor_date, get_all_vendor_names
from resolver import resolve_vendor
from intent_parser import parse_intent_llm
from query_builder import build_sql
from anomaly import detect_anomalies
from explainer import generate_explanation

class FinanceAssistantPipeline:
    def __init__(self):
        self.con = get_db_connection()
        self.anchor_date = get_anchor_date(self.con)
        self.known_vendors = get_all_vendor_names(self.con)
        print(f"Pipeline initialized. Anchor Date: {self.anchor_date}, Vendors: {len(self.known_vendors)}")

    def process_query(self, user_query: str, chat_history: list = []) -> dict:
        start_time = time.time()
        
        # 1. Parse structured intent
        intent = parse_intent_llm(user_query, chat_history, anchor_date=self.anchor_date, known_vendors=self.known_vendors)
        raw_vendor = intent.get("vendor_raw")
        
        # 2. Entity Resolution & Guardrails (Trap #2 & #4)
        vendor_match_type = "NONE"
        resolved_vendor = None
        confidence = 0.95
        
        if raw_vendor:
            vendor_match_type, resolved_entity, conf = resolve_vendor(raw_vendor, self.known_vendors)
            confidence = conf
            
            # Trap #4: Failure Mode 1 - Data does not exist
            if vendor_match_type == "NOT_FOUND":
                latency = round((time.time() - start_time) * 1000, 1)
                return {
                    "status": "NOT_FOUND",
                    "answer": f"I don't have data for vendor **'{raw_vendor}'** in our financial records. Please verify the vendor name.",
                    "table": None,
                    "sql": "-- No SQL executed (Guardrail: Vendor not found)",
                    "confidence": 0.0,
                    "confidence_label": "Entity Not Found",
                    "confidence_desc": f"No financial records exist for '{raw_vendor}'.",
                    "anomalies": [],
                    "latency_ms": latency,
                    "intent": intent
                }
                
            # Trap #4: Failure Mode 2 - Ambiguous vendor match
            elif vendor_match_type == "AMBIGUOUS":
                latency = round((time.time() - start_time) * 1000, 1)
                candidates = ", ".join([f"**{c}**" for c in resolved_entity])
                return {
                    "status": "AMBIGUOUS",
                    "answer": f"Your query is ambiguous as '{raw_vendor}' matches multiple vendors in our system: {candidates}. Which vendor did you mean?",
                    "table": None,
                    "sql": "-- No SQL executed (Guardrail: Ambiguous entity)",
                    "confidence": 0.5,
                    "confidence_label": "Ambiguous Entity",
                    "confidence_desc": f"Matches multiple vendors: {candidates}.",
                    "anomalies": [],
                    "latency_ms": latency,
                    "intent": intent
                }
            else:
                resolved_vendor = resolved_entity

        # 3. Deterministic Query Construction (Parameterized)
        query_info = build_sql(intent, resolved_vendor, self.anchor_date)
        sql = query_info["sql"]
        params = query_info.get("params", [])
        display_sql = query_info.get("display_sql", sql)

# {
#     "sql": "SELECT * FROM vendors WHERE vendor_name = %s AND date >= %s",
#     "params": ["Amazon", "2026-09-01"],
#     "display_sql": "SELECT * FROM vendors WHERE vendor_name = 'Amazon' AND date >= '2026-09-01'"
# }

        # 4. DuckDB Analytical Execution (Parameterized)
        try:
            df = self.con.execute(sql, params).df()
        except Exception as e:
            latency = round((time.time() - start_time) * 1000, 1)
            return {
                "status": "ERROR",
                "answer": f"An error occurred while executing the database query: {str(e)}",
                "table": None,
                "sql": display_sql,
                "confidence": 0.0,
                "anomalies": [],
                "latency_ms": latency,
                "intent": intent
            }

        # 5. Check Anomalies (Bonus Requirement)
        anomalies = []
        if resolved_vendor and not df.empty:
            anomalies = detect_anomalies(self.con, resolved_vendor, df, query_info)

        # 6. Generate Grounded Explanation
        explanation = generate_explanation(user_query, query_info, df, anomalies)
        
        latency = round((time.time() - start_time) * 1000, 1)

        # Explicit Confidence Signalling (Bonus Requirement)
        if confidence >= 0.95:
            conf_label = "High Certainty"
            conf_desc = "Exact entity match & 100% deterministic database computation."
        elif confidence >= 0.70:
            conf_label = "Moderate Certainty"
            conf_desc = f"Resolved vendor '{raw_vendor}' via alias/fuzzy matching."
        else:
            conf_label = "Low Certainty"
            conf_desc = "Entity or filter is ambiguous; verification recommended."

        return {
            "status": "SUCCESS",
            "answer": explanation,
            "table": df,
            "sql": display_sql,
            "confidence": confidence,
            "confidence_label": conf_label,
            "confidence_desc": conf_desc,
            "anomalies": anomalies,
            "latency_ms": latency,
            "intent": intent
        }

