"""
Agent Accuracy Evaluation
=========================
    python eval_agent.py                 # both planners
    python eval_agent.py --planner rules # no API key needed
    python eval_agent.py --model llama-3.1-8b-instant

Scores INTERPRETATION, which is the part that can actually be wrong.

Arithmetic is not evaluated here because it cannot fail in the way a benchmark
would catch: totals come from SQL, and `test_warehouse.py` already asserts them
against independently computed values. What varies is whether a question was
understood -- the right counterparty, the right window, and whether a guardrail
fired when it should have.

Every case therefore asserts the OUTCOME and the RESOLVED FILTERS, not the
wording of the answer. A case passes only if the agent reached the right
terminal state with the right scope.

Running it across models is how BUGS.md B16 gets settled: the smallest model
that scores 100% here is the one to ship.
"""

import os
import sys
import time
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import db
import session as session_mod
import agent as agent_mod
import llm


def f(res, key):
    return (res.result.filters or {}).get(key) if res.result is not None else None


# (id, category, question, follow-up context, check)
CASES = [
    (1, "Merchant spend, explicit month",
     "How much have I spent on Swiggy last month?", None,
     lambda r: r.status == agent_mod.ANSWER and f(r, "merchant") == "SWIGGY"
               and f(r, "start") == "2026-05-01" and f(r, "end") == "2026-05-31"),

    (2, "All-time total",
     "How much have I spent on Zomato total?", None,
     lambda r: r.status == agent_mod.ANSWER and f(r, "merchant") == "ZOMATO"
               and f(r, "start") is None),

    (3, "Ranking counterparties",
     "Which vendor have I spent on the most?", None,
     lambda r: r.status == agent_mod.ANSWER and r.kind == "rank"
               and f(r, "merchant") is None),

    (4, "Unnamed person -> ask which",
     "How much did my friend pay me in the last 3 months?", None,
     lambda r: r.status == agent_mod.CLARIFY and len(r.options) > 1),

    (5, "Missing period -> ask which",
     "I want to calculate my spending for swiggy", None,
     lambda r: r.status == agent_mod.CLARIFY and "period" in str(r.pending)),

    (6, "Unknown counterparty -> guardrail",
     "What did I spend on Oracle?", None,
     lambda r: r.status == agent_mod.GUARDRAIL and "Oracle" in r.answer),

    (7, "Ambiguous counterparty -> ask which",
     "How much have I spent on selection last month?", None,
     lambda r: r.status == agent_mod.CLARIFY and len(r.options) > 1),

    (8, "Typo tolerance",
     "How much have I spent on swigy last month?", None,
     lambda r: r.status == agent_mod.ANSWER and f(r, "merchant") == "SWIGGY"),

    (9, "Legal name folds onto brand",
     "How much have I spent with BUNDL TECHNOLOGIES last month?", None,
     lambda r: r.status == agent_mod.ANSWER and f(r, "merchant") == "SWIGGY"),

    (10, "Follow-up inherits merchant + period",
     "Show me these transactions",
     {"merchant": "SWIGGY", "period_token": "last_month", "direction": "debit"},
     lambda r: r.status == agent_mod.ANSWER and f(r, "merchant") == "SWIGGY"
               and f(r, "start") == "2026-05-01"),

    (11, "General question resets scope",
     "what was my last transaction in general",
     {"merchant": "SWIGGY", "period_token": "last_month", "direction": "debit"},
     lambda r: r.status == agent_mod.ANSWER and f(r, "merchant") is None),

    (12, "Comparison derives the baseline",
     "compare it to the 3 months before",
     {"merchant": "SWIGGY", "period_token": "last_3_months", "direction": "debit"},
     lambda r: r.status == agent_mod.ANSWER and r.kind == "compare"
               and "3 calendar months" in str(r.result.facts.get("period_a"))
               and "3 calendar months" in str(r.result.facts.get("period_b"))),

    (13, "Balance is one account, not ten",
     "Show my balance", None,
     lambda r: r.status == agent_mod.ANSWER and r.kind == "balances"
               and len(r.result.rows) == 1),

    (14, "Credits from a named person",
     "How much did Gautam Singh pay me in the last 3 months?", None,
     lambda r: r.status == agent_mod.ANSWER
               and f(r, "direction") == config.TXN_CREDIT
               and (f(r, "merchant") or "").startswith("GAUTAM")),

    (15, "Time phrase is not a counterparty",
     "compare my spending to the 3 months before",
     {"merchant": "SWIGGY", "period_token": "last_3_months", "direction": "debit"},
     lambda r: r.status != agent_mod.GUARDRAIL),

    # --- the long tail reported from live testing -------------------------
    (16, "Threshold: transactions over one lakh",
     "which transaction is more than one lakh", None,
     lambda r: r.status == agent_mod.ANSWER and r.kind == "list"
               and (r.result.facts.get("min_amount") == 100000
                    or r.result.filters.get("via") == "generated_sql")),

    (17, "Single highest transaction (not a vendor ranking)",
     "what was the highest amount I have done in a transaction", None,
     lambda r: r.status == agent_mod.ANSWER and r.kind in ("list", "sql")
               and len(r.result.rows) == 1),

    (18, "Latest transaction: no period question, both directions",
     "what is my latest transaction", None,
     lambda r: r.status == agent_mod.ANSWER and r.kind in ("list", "sql")
               and f(r, "direction") is None),

    (19, "Last N with a merchant: no period question",
     "last 5 transactions in bookmyshow", None,
     lambda r: r.status == agent_mod.ANSWER and f(r, "merchant") == "BOOKMYSHOW"
               and len(r.result.rows) == 5),

    (20, "Which month was highest (monthly breakdown)",
     "on which month I have high expense", None,
     lambda r: r.status == agent_mod.ANSWER
               and (r.result.facts.get("top_month") is not None
                    or r.result.filters.get("via") == "generated_sql")),

    (21, "Other account details -> balances, all accounts",
     "show me my other account details", None,
     lambda r: r.status == agent_mod.ANSWER and len(r.result.rows) > 1),

    (22, "Long tail only SQL can express (LLM) / honest note (rules)",
     "how many transactions did I make on weekends", None,
     lambda r: r.status == agent_mod.ANSWER and (
         r.result.filters.get("via") == "generated_sql"
         or any("need the language model" in n for n in r.result.notes))),

    (23, "First transaction in a calendar year",
     "Show me the first transaction in the year 2026", None,
     lambda r: r.status == agent_mod.ANSWER and f(r, "start") == "2026-01-01"
               and len(r.result.rows) == 1),

    (24, "Unknown vendor never becomes 'you spent nothing' via SQL",
     "What did I spend on Oracle?", None,
     lambda r: r.status == agent_mod.GUARDRAIL and "oracle" in r.answer.lower()),
]


def run(bot, planner_label: str, expect_planner: str = None,
        verbose: bool = True) -> dict:
    """
    `expect_planner` guards against a silently contaminated run.

    When the API is rate-limited the agent falls back to rules -- correct
    behaviour, but it means an "LLM" score would actually be measuring the
    rules planner. The run reports how many cases really used the intended
    planner so the number can be trusted or discarded.
    """
    passed, failed, latencies, actual = 0, [], [], []
    for cid, category, question, ctx, check in CASES:
        history = ([{"role": "user", "content": "(earlier question)"},
                    {"role": "assistant", "content": "(earlier answer)", "context": ctx}]
                   if ctx else [])
        t0 = time.perf_counter()
        try:
            res = bot.run(question, history=history)
            actual.append(res.planner)
            ok = bool(check(res))
            detail = f"{res.status} merchant={f(res, 'merchant')} " \
                     f"window={f(res, 'start')}..{f(res, 'end')}"
        except Exception as e:
            ok, detail = False, f"EXCEPTION {type(e).__name__}: {e}"
            actual.append("error")
        latencies.append((time.perf_counter() - t0) * 1000)

        if ok:
            passed += 1
        else:
            failed.append((cid, category, detail))
        if verbose:
            print(f"  [{'PASS' if ok else 'FAIL'}] {cid:>2}. {category:<38} {detail[:60]}")

    latencies.sort()
    on_intended = (sum(1 for p in actual if p == expect_planner or p == "resume")
                   if expect_planner else len(actual))
    return {"planner": planner_label, "passed": passed, "total": len(CASES),
            "failed": failed, "p50_ms": latencies[len(latencies) // 2],
            "max_ms": latencies[-1], "on_intended": on_intended,
            "clean": on_intended == len(CASES)}


def main():
    ap = argparse.ArgumentParser(description="Agent interpretation accuracy.")
    ap.add_argument("--planner", choices=["llm", "rules", "both"], default="both")
    ap.add_argument("--model", help="Model to evaluate (for BUGS.md B16).")
    ap.add_argument("--base-url", help="Provider endpoint. Omit to use the "
                                       "configured one; empty string means OpenAI.")
    args = ap.parse_args()

    if args.model:
        config.ACTIVE_MODEL = args.model
        os.environ["LLM_MODEL"] = args.model
    if args.base_url is not None:
        os.environ["LLM_BASE_URL"] = args.base_url
        config.LLM_BASE_URL = args.base_url
    llm.reset()

    con = db.connect(read_only=True)
    sess = session_mod.load(con)
    results = []

    wanted = ["llm", "rules"] if args.planner == "both" else [args.planner]
    for planner in wanted:
        if planner == "llm" and not llm.is_configured():
            print("\nSkipping LLM planner: no LLM_API_KEY / GROQ_API_KEY.")
            continue
        if planner == "rules":
            # Every key llm.api_key() can fall back to, or the "rules" row is
            # silently an LLM run -- which the clean? column then reports.
            for k in ("LLM_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
                os.environ[k] = ""
            config.LLM_API_KEY = ""
            config.GROQ_API_KEY = ""
            llm.reset()
            assert not llm.is_configured(), "rules run still sees an API key"

        label = (f"LLM ({llm.describe()})" if planner == "llm"
                 else "rules only (no API key)")
        print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
        bot = agent_mod.FinanceAgent(con, session=sess)
        results.append(run(bot, label, expect_planner=planner))

    print(f"\n{'=' * 74}\nSUMMARY\n{'=' * 74}")
    print(f"  {'planner':<34}{'score':>10}{'p50':>10}{'max':>10}  {'clean?':>8}")
    for r in results:
        pct = 100.0 * r["passed"] / r["total"]
        flag = "yes" if r["clean"] else f"NO ({r['on_intended']}/{r['total']})"
        print(f"  {r['planner']:<34}{r['passed']}/{r['total']} ({pct:3.0f}%)"
              f"{r['p50_ms']:>9.0f}ms{r['max_ms']:>9.0f}ms  {flag:>8}")
    if any(not r["clean"] for r in results):
        print("\n  WARNING: a run fell back to the rules planner (usually a rate")
        print("  limit). Its score does NOT measure the intended planner -- re-run")
        print("  once quota is available before quoting the number.")
    for r in results:
        for cid, cat, detail in r["failed"]:
            print(f"    FAILED [{r['planner']}] {cid}. {cat}: {detail[:70]}")

    con.close()
    return 0 if all(r["passed"] == r["total"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
