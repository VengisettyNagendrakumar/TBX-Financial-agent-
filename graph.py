"""
Agent Orchestration — LangGraph (Phase 4)
=========================================
The turn as an explicit state machine.

This replaces a hand-rolled sequence of early returns. The *domain* logic is
unchanged: every node delegates to the same `FinanceAgent` helpers
(`_plan_llm`, `_resolve_merchant`, `_gate_period`, `_confidence`, …), so
behaviour is identical and the existing suite is the proof.

    plan ─▶ inherit ─┬─▶ ask_user ─────────────────────────────▶ CLARIFY
                     ├─▶ balances ─────────────────────────────▶ ANSWER
                     └─▶ resolve_entity ─┬────────────────────▶ CLARIFY / GUARDRAIL
                                         ▼
                                   gate_person ──────────────▶ CLARIFY
                                         │
                      ┌──────────────────┴───────────┐
                      ▼                              ▼
                  compare ─▶ ANSWER          resolve_period ─▶ CLARIFY
                                                     │
                                                     ▼
                                                 execute ─▶ narrate ─▶ ANSWER

Why a graph rather than a chain: this turn is mostly *early exits*. Five of the
nine nodes can end it — an ambiguous counterparty, an unknown one, an unnamed
person, an unparseable period, a period the user never gave. Conditional edges
state that structure directly, where a linear chain would bury it in control
flow, and the guardrails are the part of this system that most needs to be
legible.

State is a plain TypedDict; `trace` uses an additive reducer so each node
appends its own audit entry without needing to know what ran before it.
"""

import operator
import re
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

import config
import db
import explainer
import queries
import resolver
from db import RESOLVED, ALL_TIME, UNRESOLVED
from queries import UnresolvedFilterError


class TurnState(TypedDict, total=False):
    """Everything one turn accumulates. `total=False` — nodes fill their part."""
    # input
    message: str
    history: list
    pending_in: dict
    # planning
    tool: str
    args: dict
    planner: str
    inherited: dict
    # resolution
    direction: Optional[str]
    raw_merchant: Optional[str]
    canonical: Optional[str]
    resolution: Any
    kind: Optional[str]
    # period
    period_token: Optional[str]
    time_range: Any
    explicit_period: bool
    # results
    result: Any
    result_kind: str
    answer: str
    narration: str
    confidence: Any
    # outcome
    status: str
    question: str
    options: list
    pending_out: dict
    # audit — additive so nodes append rather than overwrite
    trace: Annotated[list, operator.add]


def build_graph(agent):
    """
    Compiles the turn graph, closing over a FinanceAgent for domain logic.

    Nodes hold no business rules of their own; they route and delegate.
    """
    # Imported lazily to avoid a circular import at module load.
    from agent import (ANSWER, CLARIFY, GUARDRAIL, Confidence, HIGH,
                       explicit_direction, looks_like_period)

    # ---------------------------------------------------------------- plan

    def plan(state: TurnState) -> dict:
        message, pending = state["message"], state.get("pending_in") or {}

        # Resuming a clarification: fill the missing slot from this reply.
        if pending.get("slot") and pending.get("slot") in ("period", "merchant", "account"):
            merged = agent._resume(message, pending)
            if merged is None:
                # The reply did not answer the question -- ask again rather
                # than guessing at what they meant.
                return {"status": CLARIFY,
                        "question": pending.get("question", "Could you clarify?"),
                        "options": pending.get("options", []),
                        "pending_out": pending,
                        "trace": [{"step": "plan", "planner": "resume",
                                   "tool": "clarify", "args": {},
                                   "resolved": False}]}
            tool, args = merged
            return {"tool": tool, "args": args, "planner": "resume",
                    "trace": [{"step": "plan", "planner": "resume",
                               "tool": tool, "args": dict(args)}]}

        chosen = agent._plan_llm(message, state.get("history") or [])
        planner = "llm"
        if chosen is None:
            chosen = agent._plan_rules(message, state.get("history") or [])
            planner = "rules"
        tool, args = chosen
        return {"tool": tool, "args": args, "planner": planner,
                "trace": [{"step": "plan", "planner": planner,
                           "tool": tool, "args": dict(args)}]}

    def inherit(state: TurnState) -> dict:
        if state.get("planner") == "resume":
            return {"inherited": {}}
        args = state["args"]
        inherited = agent._inherit_context(
            state["message"], state["tool"], args, state.get("history") or [])
        out = {"inherited": inherited, "args": args}
        if inherited:
            out["trace"] = [{"step": "inherit_context",
                             "from_previous_turn": inherited}]
        return out

    # ------------------------------------------------------- terminal tools

    def ask_user(state: TurnState) -> dict:
        args = state["args"]
        return {"status": CLARIFY,
                "question": args.get("question", "Could you clarify?"),
                "options": args.get("options", []),
                "pending_out": {"slot": "general", "tool": "ask_user",
                                "question": args.get("question")},
                "trace": [{"step": "ask_user", "source": "planner"}]}

    def balances(state: TurnState) -> dict:
        message = state["message"]
        all_accounts = bool(re.search(
            r"\b(all|every|each|other|total|combined|across)\b.{0,20}\baccounts?\b|"
            r"\baccounts?\b.{0,20}\b(all|each|list)\b", message or "", re.I))
        r = queries.get_balances(agent.con, agent.entity_id,
                                 account_id=agent.session.account_id,
                                 all_accounts=all_accounts)
        answer, method = explainer.generate(message, "balances", r)
        return {
            "status": ANSWER, "answer": answer, "result": r,
            "result_kind": "balances", "narration": method,
            "confidence": Confidence(
                1.0, HIGH, ["Balance read directly from the account record"]),
            "trace": [{"step": "query", "tool": "get_balances",
                       "sql": r.display_sql(), "rows": len(r.rows),
                       "ms": r.latency_ms, "source": r.source,
                       "account": "all" if all_accounts else "primary"}],
        }

    # ---------------------------------------------------- entity resolution

    def resolve_entity(state: TurnState) -> dict:
        message, tool, args = state["message"], state["tool"], state["args"]

        stated = explicit_direction(message)
        direction = stated or args.get("direction")
        if tool == "list_transactions" and not stated and not args.get("_direction_inherited"):
            # "What was my last transaction?" means either direction. Letting a
            # planner's guess stand would silently hide an incoming payment.
            direction = None
        elif direction not in config.VALID_TXN_TYPES:
            direction = config.TXN_DEBIT

        raw_merchant = args.get("merchant")
        kind = args.get("kind")
        out = {"direction": direction, "kind": kind, "canonical": None,
               "resolution": None, "raw_merchant": raw_merchant, "trace": []}

        if not raw_merchant:
            return out

        # A period phrase is not a counterparty. The planner sometimes fills
        # `merchant` from "compare it to the 3 months before"; resolving that
        # can only produce a wrong vendor or a misleading not-found.
        if looks_like_period(raw_merchant):
            out["raw_merchant"] = None
            out["trace"].append({"step": "resolve_merchant", "input": raw_merchant,
                                 "status": "IGNORED_TIME_EXPRESSION"})
            return out

        if str(raw_merchant).strip().lower() in {
                "friend", "my friend", "someone", "a friend", "person"}:
            p = resolver.resolve_person(agent.con, agent.entity_id, raw_merchant)
            out["trace"].append({"step": "resolve_person", "status": p.status,
                                 "candidates": p.candidates})
            if p.status == resolver.AMBIGUOUS:
                out.update({
                    "status": CLARIFY,
                    "question": "Which person did you mean? These people have "
                                "sent you money:",
                    "options": p.candidates, "resolution": p,
                    "pending_out": {"slot": "merchant", "tool": tool,
                                    "direction": config.TXN_CREDIT,
                                    "period": args.get("period")}})
                return out
            out["kind"] = config.KIND_PERSON
            out["raw_merchant"] = None
            return out

        canonical, res = agent._resolve_merchant(raw_merchant)
        out["trace"].append({"step": "resolve_merchant", "input": raw_merchant,
                             "status": res.status if res else None,
                             "resolved": canonical,
                             "confidence": res.confidence if res else None,
                             "method": res.method if res else None})
        out["canonical"], out["resolution"] = canonical, res

        if res and res.status == resolver.AMBIGUOUS:
            out.update({"status": CLARIFY, "question": resolver.describe(res),
                        "options": res.candidates,
                        "pending_out": {"slot": "merchant", "tool": tool,
                                        "direction": direction,
                                        "period": args.get("period")}})
        elif res and res.status == resolver.NOT_FOUND:
            near = (f" The closest names on record are "
                    f"{', '.join(res.candidates[:3])}." if res.candidates else "")
            out.update({
                "status": GUARDRAIL,
                "answer": f"I have no transactions for **{raw_merchant}**.{near}",
                "confidence": Confidence(
                    1.0, HIGH,
                    [f"'{raw_merchant}' is not present in this customer's "
                     f"transaction history"])})
        return out

    def gate_person(state: TurnState) -> dict:
        """
        "How much did MY FRIEND pay me" asks about ONE person. Totalling every
        individual answers a different question while reading as authoritative.

        Checked here rather than inside a tool branch so it applies whichever
        tool the planner picked -- a gate that guards one path is not a gate.
        """
        gate = agent._gate_singular_person(
            state["message"], state["args"], state["tool"], state.get("canonical"))
        if gate is None:
            return {}
        return {"status": CLARIFY, "question": gate.question,
                "options": gate.options, "pending_out": gate.pending,
                "trace": [{"step": "policy_gate", "gate": "singular_person"}]}

    # ------------------------------------------------------------- periods

    def _unresolved(tr, tool, args):
        """The CLARIFY a bad period produces, identical on every path."""
        return {"status": CLARIFY,
                "question": f"I couldn't work out the time period "
                            f"**'{tr.label}'**. Which period did you mean?",
                "options": ["Last month", "Last 3 months", "This year", "All time"],
                "pending_out": {"slot": "period", "tool": tool,
                                **{k: v for k, v in args.items() if k != "period"}},
                "trace": [{"step": "resolve_period", "status": UNRESOLVED,
                           "input": tr.label}]}

    def compare(state: TurnState) -> dict:
        tool, args = state["tool"], state["args"]

        # The subject window. Falls back to whatever the previous turn was
        # about, so "how does that compare..." keeps its referent.
        tr_b, explicit_b = agent._resolve_period(
            args.get("period_b") or args.get("period"))
        if tr_b.status == UNRESOLVED:
            return _unresolved(tr_b, tool, args)

        tr_a, _ = agent._resolve_period(args.get("period_a"))

        # Derive the baseline instead of parsing it. "the 3 months before that"
        # has no natural token -- `previous_3_months` reads as a synonym of
        # `last_3_months` and resolves to the same range -- so an absent,
        # unparseable, or identical baseline becomes the equal-length window
        # immediately before the subject. That makes every phrasing of "the
        # period before" work without enumerating them.
        derived = None
        if args.get("period_a") is None:
            derived = "absent"
        elif tr_a.status == UNRESOLVED:
            derived = "unparseable"
        elif db.same_window(tr_a, tr_b):
            derived = "same_as_subject"
        if derived:
            tr_a = db.previous_window(tr_b)
            if tr_a.status == UNRESOLVED:
                return _unresolved(tr_a, tool, args)

        direction, canonical = state["direction"], state.get("canonical")
        res = state.get("resolution")
        r = queries.compare_periods(agent.con, agent.entity_id, direction,
                                    tr_a, tr_b, merchant=canonical)
        answer, method = explainer.generate(state["message"], "compare", r, res)
        return {
            "status": ANSWER, "answer": answer, "result": r,
            "result_kind": "compare", "narration": method,
            "confidence": agent._confidence(res, tr_b, True, r, method),
            "pending_out": {"merchant": canonical,
                            "period_token": tr_b.canonical,
                            "direction": direction},
            "trace": [{"step": "resolve_period", "baseline": tr_a.label,
                       "subject": tr_b.label,
                       "baseline_derived": derived or "as_given"},
                      {"step": "query", "tool": tool, "sql": r.display_sql(),
                       "rows": len(r.rows), "ms": r.latency_ms,
                       "source": r.source}],
        }

    def resolve_period(state: TurnState) -> dict:
        tool, args = state["tool"], state["args"]
        period_token = args.get("period")
        tr, explicit = agent._resolve_period(period_token)
        if tr.status == UNRESOLVED:
            return _unresolved(tr, tool, args)

        gate = agent._gate_period(state.get("canonical"), period_token, tr, explicit)
        if gate is not None:
            gate.pending.update({"tool": tool, "direction": state["direction"]})
            return {"status": CLARIFY, "question": gate.question,
                    "options": gate.options, "pending_out": gate.pending,
                    "resolution": state.get("resolution"),
                    "trace": [{"step": "policy_gate", "gate": "period_required",
                               "merchant": state.get("canonical")}]}

        return {"time_range": tr, "explicit_period": explicit,
                "period_token": period_token,
                "trace": [{"step": "resolve_period", "input": period_token,
                           "status": tr.status,
                           "window": f"{tr.start}..{tr.end}",
                           "label": tr.label,
                           "month_aligned": tr.month_aligned}]}

    # ------------------------------------------------------------- execute

    def execute(state: TurnState) -> dict:
        tool, args, tr = state["tool"], state["args"], state["time_range"]
        direction, canonical = state["direction"], state.get("canonical")
        kind = state.get("kind")

        if tool == "rank_counterparties":
            r = queries.top_counterparties(agent.con, agent.entity_id, direction, tr,
                                           limit=int(args.get("limit") or 10),
                                           kind=kind)
            k = "rank"
        elif tool == "list_transactions":
            r = queries.list_transactions(agent.con, agent.entity_id, direction, tr,
                                          merchant=canonical, kind=kind,
                                          limit=int(args.get("limit") or 50))
            k = "list"
        else:
            r = queries.query_spend(agent.con, agent.entity_id, direction, tr,
                                    merchant=canonical, kind=kind)
            k = "spend"

        return {"result": r, "result_kind": k,
                "trace": [{"step": "query", "tool": tool, "sql": r.display_sql(),
                           "rows": len(r.rows), "ms": r.latency_ms,
                           "source": r.source,
                           "grand_total": r.facts.get("grand_total"),
                           "truncated": r.truncated}]}

    def narrate(state: TurnState) -> dict:
        r, res, tr = state["result"], state.get("resolution"), state["time_range"]
        answer, method = explainer.generate(
            state["message"], state["result_kind"], r, res)
        conf = agent._confidence(res, tr, state.get("explicit_period", False),
                                 r, method)

        # Everything a follow-up may need to inherit.
        ctx = {"direction": state["direction"]}
        if state.get("canonical"):
            ctx["merchant"] = state["canonical"]
        if state.get("period_token"):
            ctx["period_token"] = state["period_token"]
        elif tr.status == RESOLVED and tr.canonical:
            ctx["period_token"] = tr.canonical

        return {"status": ANSWER, "answer": answer, "narration": method,
                "confidence": conf, "pending_out": ctx,
                "trace": [{"step": "narrate", "method": method},
                          {"step": "confidence", "score": conf.score,
                           "label": conf.label, "reasons": conf.reasons}]}

    # ---------------------------------------------------------- wiring

    def stop_or(next_node):
        """Any node may end the turn by setting `status`."""
        def route(state: TurnState):
            return END if state.get("status") else next_node
        return route

    def route_tool(state: TurnState):
        if state.get("status"):
            return END
        tool = state.get("tool")
        if tool == "ask_user":
            return "ask_user"
        if tool == "get_balances":
            return "balances"
        return "resolve_entity"

    def route_period(state: TurnState):
        if state.get("status"):
            return END
        return "compare" if state.get("tool") == "compare_spend" else "resolve_period"

    g = StateGraph(TurnState)
    for name, fn in [("plan", plan), ("inherit", inherit), ("ask_user", ask_user),
                     ("balances", balances), ("resolve_entity", resolve_entity),
                     ("gate_person", gate_person), ("compare", compare),
                     ("resolve_period", resolve_period), ("execute", execute),
                     ("narrate", narrate)]:
        g.add_node(name, fn)

    g.set_entry_point("plan")
    g.add_conditional_edges("plan", stop_or("inherit"), {"inherit": "inherit", END: END})
    g.add_conditional_edges("inherit", route_tool,
                            {"ask_user": "ask_user", "balances": "balances",
                             "resolve_entity": "resolve_entity", END: END})
    g.add_edge("ask_user", END)
    g.add_edge("balances", END)
    g.add_conditional_edges("resolve_entity", stop_or("gate_person"),
                            {"gate_person": "gate_person", END: END})
    g.add_conditional_edges("gate_person", route_period,
                            {"compare": "compare",
                             "resolve_period": "resolve_period", END: END})
    g.add_edge("compare", END)
    g.add_conditional_edges("resolve_period", stop_or("execute"),
                            {"execute": "execute", END: END})
    g.add_edge("execute", "narrate")
    g.add_edge("narrate", END)

    return g.compile()


def ascii_diagram(compiled) -> str:
    """The compiled topology, for documentation and debugging."""
    try:
        return compiled.get_graph().draw_ascii()
    except Exception as e:  # optional dependency (grandalf)
        return f"(diagram unavailable: {e})"
