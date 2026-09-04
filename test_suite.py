"""
Automated Test Suite
====================
Runs 10 comprehensive edge cases covering:
1. Normal spend summary
2. Relative date ranges ("last month", "YTD")
3. Fuzzy vendor lookup (alias resolution)
4. Ambiguous vendor guardrail (Failure Mode 2)
5. Missing vendor guardrail (Failure Mode 1)
6. Non-binary reconciliation audit (unreconciled, pending, disputed)
7. Spend anomaly detection (Acme Corp spike)
8. Multi-turn context persistence
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pipeline import FinanceAssistantPipeline

def run_tests():
    print("=" * 70)
    print("STARTING AUTOMATED TEST SUITE FOR FINANCE ASSISTANT")
    print("=" * 70)
    
    pipeline = FinanceAssistantPipeline()
    
    test_cases = [
        {
            "id": 1,
            "name": "Standard Spend Summary",
            "query": "How much did we spend on Acme Corporation in May 2024?",
            "expected_status": "SUCCESS",
            "check": lambda res: res["table"] is not None and len(res["table"]) > 0
        },
        {
            "id": 2,
            "name": "Relative Date Calculation ('last month')",
            "query": "What was our total spend on AWS last month?",
            "expected_status": "SUCCESS",
            "check": lambda res: "2024-04" in res["sql"] or "BETWEEN '2024-04-01' AND '2024-04-30'" in res["sql"]
        },
        {
            "id": 3,
            "name": "Fuzzy Vendor Resolution ('AWS' -> 'Amazon Web Services, Inc.')",
            "query": "Show payouts to AWS",
            "expected_status": "SUCCESS",
            "check": lambda res: "Amazon Web Services, Inc." in res["sql"]
        },
        {
            "id": 4,
            "name": "Ambiguous Vendor Guardrail (Failure Mode 2)",
            "query": "How much did we spend on Amazon?",
            "expected_status": "AMBIGUOUS",
            "check": lambda res: "Amazon Web Services" in res["answer"] and "Amazon Logistics" in res["answer"]
        },
        {
            "id": 5,
            "name": "Missing Data Guardrail (Failure Mode 1)",
            "query": "What did we pay Netflix last month?",
            "expected_status": "NOT_FOUND",
            "check": lambda res: "netflix" in res["answer"].lower() and res["table"] is None
        },
        {
            "id": 6,
            "name": "Reconciliation Audit",
            "query": "Which transactions are still unreconciled?",
            "expected_status": "SUCCESS",
            "check": lambda res: res["table"] is not None and "reconciliation_status" in res["sql"].lower()
        },
        {
            "id": 7,
            "name": "Pending / Disputed Reconciliation Statuses",
            "query": "Show pending reconciliation transactions",
            "expected_status": "SUCCESS",
            "check": lambda res: res["table"] is not None
        },
        {
            "id": 8,
            "name": "Anomaly Detection Callout (Acme Corp May Spike)",
            "query": "Show all payouts to Acme Corporation",
            "expected_status": "SUCCESS",
            "check": lambda res: len(res["anomalies"]) > 0
        },
        {
            "id": 9,
            "name": "Category Spend Summary",
            "query": "Show total spend by category",
            "expected_status": "SUCCESS",
            "check": lambda res: res["table"] is not None and "category" in res["sql"].lower()
        },
        {
            "id": 10,
            "name": "Zero Match Category Guardrail",
            "query": "Show spend for category NonExistentCategory",
            "expected_status": "SUCCESS",
            "check": lambda res: res["table"] is not None and len(res["table"]) == 0
        },
        {
            "id": 11,
            "name": "Dynamic Unknown Vendor Fallback Guardrail",
            "query": "What did we spend on O'Brien Consulting?",
            "expected_status": "NOT_FOUND",
            "check": lambda res: "o'brien" in res["answer"].lower() and res["table"] is None
        }
    ]

    passed = 0
    failed = 0

    for tc in test_cases:
        print(f"\n[Test {tc['id']}] {tc['name']}")
        print(f"Query: \"{tc['query']}\"")
        res = pipeline.process_query(tc["query"])
        
        status_ok = (res["status"] == tc["expected_status"])
        check_ok = tc["check"](res)
        
        if status_ok and check_ok:
            passed += 1
            print(f"[PASS] Latency: {res['latency_ms']}ms | Confidence: {res['confidence']}")
            clean_ans = res['answer'][:110].replace("\n", " ")
            print(f"   Answer: {clean_ans}...")
            if res.get("anomalies"):
                clean_alert = res['anomalies'][0].replace("⚠️", "[ALERT]")
                print(f"   Alert: {clean_alert}")
        else:
            failed += 1
            print(f"[FAIL] Status: {res['status']} (Expected: {tc['expected_status']})")
            print(f"   Answer: {res['answer']}")
            print(f"   SQL: {res['sql']}")

    # Multi-turn test
    print("\n[Test 12] Multi-Turn Conversation (Context Carry)")
    turn1_query = "What did we spend on CloudScale in April?"
    print(f"Turn 1 Query: \"{turn1_query}\"")
    res1 = pipeline.process_query(turn1_query)
    print(f"Turn 1 Answer: {res1['answer'][:100]}...")
    
    turn2_query = "What about in May?"
    print(f"Turn 2 Query: \"{turn2_query}\" (relying on prior vendor context)")
    chat_history = [
        {"role": "user", "content": turn1_query},
        {"role": "assistant", "content": res1["answer"]}
    ]
    res2 = pipeline.process_query(turn2_query, chat_history=chat_history)
    
    if "CloudScale Technologies" in res2["sql"] and "2024-05" in res2["sql"]:
        passed += 1
        print(f"[PASS] Retained vendor 'CloudScale Technologies' in Turn 2!")
        print(f"Turn 2 Answer: {res2['answer'][:100]}...")
    else:
        failed += 1
        print(f"[FAIL] Multi-turn test | SQL: {res2['sql']}")

    # Explicit SQL Injection hardening verification (Peer-Review Issue #1)
    print("\n[Security Verification] SQL Injection Parameterization Defense")
    from query_builder import build_sql
    injection_intent = {"intent": "category_spend", "category": "Cloud' OR '1'='1"}
    injected_info = build_sql(injection_intent, None, pipeline.anchor_date)
    has_param_placeholder = ("?" in injected_info["sql"])
    has_bound_param = any("cloud' or '1'='1" in str(p) for p in injected_info["params"])
    injected_df = pipeline.con.execute(injected_info["sql"], injected_info["params"]).df()
    injection_safe = has_param_placeholder and has_bound_param and len(injected_df) == 0
    
    if injection_safe:
        passed += 1
        print("[PASS] SQL Parameterization successfully neutralized malicious injection payload 'Cloud\\' OR \\'1\\'=\\'1'!")
    else:
        failed += 1
        print("[FAIL] SQL Injection vulnerability detected!")

    print("\n" + "=" * 70)
    print(f"TEST SUMMARY: {passed} PASSED, {failed} FAILED (Total: {passed + failed})")
    print("=" * 70)
    
    return failed == 0

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
