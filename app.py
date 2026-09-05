"""
Finance Assistant — Chat UI (Phase 4)
=====================================
    streamlit run app.py

Streamlit is the Phase 4 surface. Phase 5 moves transport to FastAPI behind
TLS, since Streamlit cannot terminate HTTPS itself; the agent underneath is
unchanged by that move.

What the UI is responsible for:
  - selecting the entity (the session's identity; the model never sees it)
  - rendering clarifying questions as clickable options and resuming the turn
  - showing the resolved interpretation, so a wrong window is visible rather
    than silent (the lesson of BUGS.md B01)
  - the audit trace: every tool call, the SQL, timings, and whether the
    narration was model-written or a verified fallback
"""

import os
import re
import html
import streamlit as st

import config
import db
import agent as agent_mod
import explainer
import session as session_mod

st.set_page_config(page_title="Finance Assistant", page_icon="💸",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
  .stApp { background: #0d1117; }
  div[data-testid="stMetric"] { background:#161b22; border:1px solid #30363d;
      border-radius:10px; padding:12px 14px; }
  .interp { background:#12203a; border-left:3px solid #388bfd; padding:8px 12px;
      border-radius:4px; font-size:0.86rem; color:#c9d1d9; margin:6px 0 10px; }
  .note { background:#1c1a10; border-left:3px solid #d29922; padding:8px 12px;
      border-radius:4px; font-size:0.84rem; color:#e3d5a8; margin:4px 0; }
  .grd { font-size:0.78rem; color:#7d8590; margin-top:6px; }
  .conf { display:inline-block; font-size:0.8rem; padding:3px 10px; border-radius:12px;
      margin:6px 0; border:1px solid; }
  .conf-sub { opacity:0.75; font-weight:400; }
  .conf-hi { color:#3fb950; border-color:#238636; background:#0f2417; }
  .conf-md { color:#d29922; border-color:#9e6a03; background:#241d0f; }
  .conf-lo { color:#f85149; border-color:#8b2c25; background:#2a1416; }
</style>
""", unsafe_allow_html=True)


def esc(text: str) -> str:
    """
    Escapes HTML, then neutralises '$' so Streamlit's KaTeX does not treat
    'Rs 1,234 ... Rs 5,678' as inline maths and swallow the text between.
    """
    if not text:
        return ""
    s = html.escape(str(text))
    return s.replace("$", "&#36;")

def esc_inline(text: str) -> str:
    """
    Same, but keeps **bold** working.

    Markdown is not processed inside a raw HTML block, so the banners rendered
    via <div> would otherwise show literal asterisks.
    """
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(text))


@st.cache_resource(show_spinner=False)
def get_connection():
    return db.connect(read_only=True)


@st.cache_resource(show_spinner=False)
def get_session(_con):
    """The logged-in customer. Hardcoded in session.py for the prototype."""
    return session_mod.load(_con)


if not os.path.exists(config.WAREHOUSE_PATH):
    st.error("No warehouse found. Build one first:")
    st.code("python data_generator.py --rows 200000\npython build_warehouse.py", language="bash")
    st.stop()

con = get_connection()
sess = get_session(con)
entity_id = sess.entity_id
anchor = db.get_anchor_date(con)

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("💸 Finance Assistant")
    st.caption("Ask about your spending in plain language.")

    st.markdown(f"**Signed in as** {sess.display_name}")
    st.caption(f"Primary account **{sess.masked_number}** · {sess.bank_name}  \n"
               f"{sess.account_count} account(s) on file")

    st.caption(f"Data through **{anchor}** — relative dates resolve against this, "
               f"not today's date.")

    st.divider()
    st.caption("**Try asking**")
    for q in ["How much have I spent on Swiggy last month?",
              "How much have I spent on Zomato total?",
              "Which vendor have I spent on the most?",
              "How much did my friend pay me in the last 3 months?",
              "I want to calculate my spending for swiggy",
              "How does that compare to the month before?",
              "What did I spend on Oracle?",
              "Show my balance"]:
        if st.button(q, key=f"s_{q}", use_container_width=True):
            st.session_state.queued = q

    st.divider()
    llm_on = bool(os.getenv("GROQ_API_KEY", config.GROQ_API_KEY))
    st.caption(f"Planner: **{'LLM + rules fallback' if llm_on else 'rules only (no API key)'}**")
    st.caption(f"Model: `{config.ACTIVE_MODEL}`")

    with st.expander("📄 Note on Model Choice (Section 7 Compliant)"):
        st.markdown(f"**Active Model**: `{config.ACTIVE_MODEL}` (Groq)")
        st.markdown("**Section 7 Constraint Compliance**:")
        st.caption("• **<= 20B Upper Limit**: `openai/gpt-oss-20b` adheres strictly to the 20B parameter ceiling.\n• **20M Record Scalability**: Arithmetic and joins are offloaded to vectorized DuckDB OLAP, built for 20M+ records.\n• **Cost & Speed**: ~$0.0002/query at 500+ tok/s with <5ms database math.")

    if st.button("🗑 Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.pending = None
        st.rerun()

# ---------------------------------------------------------------- state
if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending" not in st.session_state:
    st.session_state.pending = None



@st.cache_resource(show_spinner=False)
def get_agent(_con, _sess, entity):
    return agent_mod.FinanceAgent(_con, session=_sess)


bot = get_agent(con, sess, entity_id)

st.title("Conversational Finance Assistant")
st.caption("Every number is computed by the database. The model explains results — "
           "it never calculates them, and figures it cannot ground are discarded.")


def render(msg, key):
    """Renders one assistant turn."""
    st.markdown(esc(msg.get("content", "")), unsafe_allow_html=True)

    conf = msg.get("confidence")
    if conf:
        # Band only. A precise-looking percentage invites the reader to treat a
        # combination of heuristics as a measurement; the band plus the reasons
        # says what is actually known.
        cls = {"High": "conf-hi", "Medium": "conf-md"}.get(conf["label"], "conf-lo")
        dot = {"High": "●", "Medium": "◐"}.get(conf["label"], "○")
        blurb = {
            "High": "interpreted unambiguously",
            "Medium": "one or more details were assumed",
            "Low": "worth checking the interpretation below",
        }.get(conf["label"], "")
        st.markdown(
            f"<div class='conf {cls}'>{dot} <strong>{conf['label']} confidence</strong>"
            f"<span class='conf-sub'> — {blurb}</span></div>",
            unsafe_allow_html=True)
        if conf.get("reasons"):
            with st.expander("How was this confidence determined?"):
                for reason in conf["reasons"]:
                    st.markdown(f"- {reason}")
                st.caption("Amounts are computed by the database and are exact. "
                           "Confidence reflects how the question was interpreted — "
                           "the counterparty, the date window, and how much of the "
                           "underlying data could be attributed.")

    if msg.get("interpretation"):
        st.markdown(f"<div class='interp'>{esc_inline(msg['interpretation'])}</div>",
                    unsafe_allow_html=True)
    if msg.get("inherited"):
        bits = ", ".join(f"{k} = **{v}**" for k, v in msg["inherited"].items())
        st.markdown(f"<div class='interp'>↩ Carried over from your previous "
                    f"question: {esc_inline(bits)}</div>", unsafe_allow_html=True)
    for n in msg.get("notes", []):
        st.markdown(f"<div class='note'>⚠ {esc_inline(n)}</div>", unsafe_allow_html=True)

    facts = msg.get("facts") or {}
    if facts.get("grand_total") is not None and msg.get("kind") != "list":
        c = st.columns(4)
        c[0].metric("Total", explainer.money(facts["grand_total"]))
        if facts.get("txn_count") is not None:
            c[1].metric("Transactions", f"{facts['txn_count']:,}")
        if facts.get("average"):
            c[2].metric("Average", explainer.money(facts["average"]))
        if facts.get("group_count"):
            c[3].metric("Counterparties", f"{facts['group_count']:,}")

    rows = msg.get("rows")
    if rows is not None and not rows.empty:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.download_button("⬇ Export CSV", rows.to_csv(index=False).encode("utf-8"),
                           file_name="transactions.csv", mime="text/csv",
                           key=f"dl_{key}")

    if msg.get("trace"):
        with st.expander("🔍 Audit trace"):
            for step in msg["trace"]:
                s = step.get("step")
                if s == "plan":
                    st.markdown(f"**1. Plan** — planner `{step['planner']}` → "
                                f"tool `{step['tool']}`")
                    st.json(step["args"], expanded=False)
                elif s == "resolve_merchant":
                    st.markdown(f"**Resolve counterparty** — `{step['input']}` → "
                                f"**{step.get('resolved')}** "
                                f"({step.get('status')}, {step.get('method')}, "
                                f"confidence {step.get('confidence')})")
                elif s == "resolve_person":
                    st.markdown(f"**Resolve person** — {step.get('status')}: "
                                f"{step.get('candidates')}")
                elif s == "resolve_period":
                    st.markdown(f"**Resolve period** — `{step.get('input')}` → "
                                f"**{step.get('label')}** "
                                f"(`{step.get('window')}`, month-aligned: "
                                f"{step.get('month_aligned')})")
                elif s == "policy_gate":
                    st.markdown(f"**Policy gate fired** — `{step.get('gate')}` "
                                f"→ asked the user instead of guessing")
                elif s == "query":
                    st.markdown(f"**Query** — source `{step.get('source')}`, "
                                f"{step.get('rows')} rows, {step.get('ms')} ms")
                    st.code(step.get("sql", ""), language="sql")
                elif s == "narrate":
                    st.markdown(f"**Narration** — `{step.get('method')}`")
            meta = msg.get("meta", {})
            st.caption(f"Turn latency **{meta.get('latency_ms', 0):.0f} ms** · "
                       f"planner **{meta.get('planner')}** · "
                       f"narration **{meta.get('narration')}** · "
                       f"confidence **{(msg.get('confidence') or {}).get('label', 'High')}**")

    if msg.get("narration") == "llm_rejected":
        st.markdown("<div class='grd'>⚑ The model produced a figure the database "
                    "did not return, so its wording was discarded and a verified "
                    "template was used instead.</div>", unsafe_allow_html=True)


for i, m in enumerate(st.session_state.messages):
    with st.chat_message(m["role"]):
        if m["role"] == "user":
            st.markdown(esc(m["content"]), unsafe_allow_html=True)
        else:
            render(m, key=i)

# ---------------------------------------------------------------- input
prompt = st.chat_input("Ask about your spending…")
if st.session_state.get("queued"):
    prompt = st.session_state.pop("queued")
if st.session_state.get("chosen"):
    prompt = st.session_state.pop("chosen")

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(esc(prompt), unsafe_allow_html=True)

    history = [{"role": m["role"], "content": m.get("content", ""),
                "context": m.get("context", {})}
               for m in st.session_state.messages]

    with st.chat_message("assistant"):
        with st.spinner("Querying…"):
            res = bot.run(prompt, history=history, pending=st.session_state.pending)

        msg = {"role": "assistant", "trace": res.trace,
               "meta": {"latency_ms": res.latency_ms, "planner": res.planner,
                        "narration": res.narration},
               "narration": res.narration, "kind": res.kind,
               "context": res.pending or {},
               "inherited": res.inherited or {},
               "confidence": {"pct": res.confidence.pct,
                              "label": res.confidence.label,
                              "reasons": res.confidence.reasons}}

        if res.status == agent_mod.CLARIFY:
            st.session_state.pending = {**(res.pending or {}),
                                        "question": res.question,
                                        "options": res.options}
            msg["content"] = res.question
            msg["options"] = res.options
        else:
            st.session_state.pending = None
            msg["content"] = res.answer
            if res.result is not None:
                msg["rows"] = res.result.rows
                msg["facts"] = res.result.facts
                msg["notes"] = res.result.notes
                fl = res.result.filters or {}
                bits = []
                if fl.get("merchant"):
                    bits.append(f"counterparty **{fl['merchant']}**")
                if fl.get("period"):
                    bits.append(f"period **{fl['period']}**")
                if fl.get("direction"):
                    bits.append("money **out**" if fl["direction"] == config.TXN_DEBIT
                                else "money **in**")
                if bits:
                    msg["interpretation"] = "Interpreted as " + ", ".join(bits) + "."

        st.session_state.messages.append(msg)
        render(msg, key=len(st.session_state.messages))

# Clarification options render as buttons on the latest assistant turn.
last = st.session_state.messages[-1] if st.session_state.messages else None
if last and last.get("role") == "assistant" and last.get("options"):
    cols = st.columns(min(len(last["options"]), 6))
    for i, opt in enumerate(last["options"][:6]):
        if cols[i % len(cols)].button(opt, key=f"opt_{len(st.session_state.messages)}_{i}",
                                      use_container_width=True):
            st.session_state.chosen = opt
            st.rerun()
