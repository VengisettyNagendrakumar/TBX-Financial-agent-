"""
Chat Persistence — threads on local disk
========================================
Multiple conversations, each keyed by a LangGraph `thread_id`, surviving a
page refresh and a restart.

Two layers share one SQLite file:

  1. LangGraph's SqliteSaver         -- graph execution state per thread_id.
     This is what makes a clarification durable: the pending question survives
     the process, not just the browser tab (ARCHITECTURE_V2.md §14.9).

  2. `threads` / `messages` here     -- the renderable transcript. The
     checkpointer stores what the GRAPH needs to resume; it does not store what
     the UI needs to redraw a conversation (the answer text, the metric tiles,
     the audit trace). Those are different concerns and conflating them would
     mean re-running queries just to repaint a screen.

Kept in its own file rather than inside warehouse.duckdb so that
`ingest.py --purge` can wipe the dummy financial data without destroying the
conversations -- and so a corrupt warehouse never takes the chat list with it.
"""

import os
import json
import time
import uuid
import sqlite3
import threading
from datetime import date, datetime

import config

_LOCK = threading.Lock()

MAX_STORED_ROWS = 200      # cap the table snapshot kept per message
TITLE_MAX = 60


def _json_default(o):
    """Makes DataFrame records and numpy scalars JSON-safe."""
    if isinstance(o, (datetime, date)):
        return str(o)
    if hasattr(o, "item"):          # numpy scalar
        try:
            return o.item()
        except Exception:
            pass
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def dumps(obj) -> str:
    return json.dumps(obj, default=_json_default, allow_nan=False)


def _safe_loads(text, fallback=None):
    try:
        return json.loads(text) if text else (fallback if fallback is not None else {})
    except Exception:
        return fallback if fallback is not None else {}


def frame_to_records(df, limit: int = MAX_STORED_ROWS):
    """A JSON-safe snapshot of a result table, capped for storage."""
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for rec in df.head(limit).to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if v is None:
                clean[str(k)] = None
            elif isinstance(v, float) and v != v:      # NaN
                clean[str(k)] = None
            elif hasattr(v, "item"):
                try:
                    clean[str(k)] = v.item()
                except Exception:
                    clean[str(k)] = str(v)
            elif isinstance(v, (datetime, date)):
                clean[str(k)] = str(v)
            elif isinstance(v, (int, float, str, bool)):
                clean[str(k)] = v
            else:
                clean[str(k)] = str(v)
        out.append(clean)
    return out


def make_title(text: str) -> str:
    """First user message, trimmed -- the conversation's name until renamed."""
    t = " ".join(str(text or "").split())
    if not t:
        return "New chat"
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1].rstrip() + "…"


class ChatStore:
    def __init__(self, path: str = None):
        self.path = path or config.CHAT_DB_PATH
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Streamlit runs each session on its own thread; the module-level lock
        # serialises writes since a sqlite3 connection is not thread-safe.
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with _LOCK:
            self.con.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id   TEXT PRIMARY KEY,
                    entity_id   TEXT,
                    title       TEXT,
                    created_at  REAL,
                    updated_at  REAL,
                    pending     TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    thread_id   TEXT NOT NULL,
                    seq         INTEGER NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT,
                    payload     TEXT,
                    created_at  REAL,
                    PRIMARY KEY (thread_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, seq);
                CREATE INDEX IF NOT EXISTS idx_thread_entity
                    ON threads(entity_id, updated_at DESC);
            """)
            self.con.commit()

    # ---------------------------------------------------------- threads

    def new_thread(self, entity_id: str, title: str = "New chat") -> str:
        thread_id = uuid.uuid4().hex
        now = time.time()
        with _LOCK:
            self.con.execute(
                "INSERT INTO threads (thread_id, entity_id, title, created_at, "
                "updated_at, pending) VALUES (?,?,?,?,?,NULL)",
                (thread_id, entity_id, title, now, now))
            self.con.commit()
        return thread_id

    def list_threads(self, entity_id: str, limit: int = 50) -> list:
        with _LOCK:
            rows = self.con.execute("""
                SELECT t.thread_id, t.title, t.created_at, t.updated_at,
                       (SELECT COUNT(*) FROM messages m
                         WHERE m.thread_id = t.thread_id) AS message_count
                FROM threads t
                WHERE t.entity_id = ?
                ORDER BY t.updated_at DESC LIMIT ?
            """, (entity_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def get_thread(self, thread_id: str):
        with _LOCK:
            row = self.con.execute(
                "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def rename_thread(self, thread_id: str, title: str):
        with _LOCK:
            self.con.execute(
                "UPDATE threads SET title = ?, updated_at = ? WHERE thread_id = ?",
                (make_title(title), time.time(), thread_id))
            self.con.commit()

    def delete_thread(self, thread_id: str):
        """Removes the transcript and every LangGraph checkpoint for the thread."""
        with _LOCK:
            self.con.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
            self.con.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
            # Graph threads are derived per turn as "<conversation>#<turn>",
            # so a conversation's checkpoints are matched by prefix.
            for table in ("checkpoints", "writes", "checkpoint_blobs",
                          "checkpoint_writes"):
                try:
                    self.con.execute(
                        f"DELETE FROM {table} WHERE thread_id = ? OR thread_id LIKE ?",
                        (thread_id, f"{thread_id}#%"))
                except sqlite3.OperationalError:
                    pass  # checkpointer tables are created lazily
            self.con.commit()

    def touch(self, thread_id: str):
        with _LOCK:
            self.con.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?",
                             (time.time(), thread_id))
            self.con.commit()

    # ------------------------------------------------------- clarification

    def set_pending(self, thread_id: str, pending: dict):
        """
        The open clarifying question, so a refresh does not lose it.

        Without this a user answering "Last 3 months" after a reload would have
        the reply interpreted as a brand-new question.
        """
        with _LOCK:
            self.con.execute(
                "UPDATE threads SET pending = ?, updated_at = ? WHERE thread_id = ?",
                (dumps(pending) if pending else None, time.time(), thread_id))
            self.con.commit()

    def get_pending(self, thread_id: str) -> dict:
        row = self.get_thread(thread_id)
        return _safe_loads(row.get("pending")) if row else {}

    # ----------------------------------------------------------- messages

    def append_message(self, thread_id: str, role: str, content: str,
                       payload: dict = None) -> int:
        with _LOCK:
            seq = self.con.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE thread_id = ?",
                (thread_id,)).fetchone()[0]
            self.con.execute(
                "INSERT INTO messages (thread_id, seq, role, content, payload, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (thread_id, seq, role, content,
                 dumps(payload) if payload else None, time.time()))
            self.con.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?",
                             (time.time(), thread_id))
            self.con.commit()
        return seq

    def get_messages(self, thread_id: str) -> list:
        with _LOCK:
            rows = self.con.execute(
                "SELECT role, content, payload FROM messages "
                "WHERE thread_id = ? ORDER BY seq", (thread_id,)).fetchall()
        out = []
        for r in rows:
            msg = {"role": r["role"], "content": r["content"]}
            msg.update(_safe_loads(r["payload"]))
            out.append(msg)
        return out

    def history_for_agent(self, thread_id: str, limit: int = 8) -> list:
        """
        The compact form the planner needs: role, content, and the filters an
        answer used, so follow-ups can inherit them after a restart.
        """
        msgs = self.get_messages(thread_id)[-limit:]
        return [{"role": m["role"], "content": m.get("content", ""),
                 "context": m.get("context", {})} for m in msgs]

    def stats(self) -> dict:
        with _LOCK:
            t = self.con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            m = self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"threads": t, "messages": m, "bytes": size, "path": self.path}

    # ------------------------------------------------------- checkpointer

    def checkpointer(self):
        """
        LangGraph's SqliteSaver over the same file.

        Sharing the connection keeps thread deletion atomic: the transcript and
        the graph checkpoints for a thread go in one transaction.

        The graph state is plain JSON types only. Rich objects (QueryResult,
        Resolution, TimeRange, Confidence) live in agent._scratch[run_id] for
        the duration of a turn and never reach the checkpointer. That was not
        always so: they used to ride in state under `pickle_fallback`, and the
        first time Streamlit hot-reloaded queries.py every turn failed with
        "Can't pickle QueryResult: it's not the same object as
        queries.QueryResult" -- pickle resolves classes by import path, and a
        reloaded module is a different class object. Keeping state primitive
        makes checkpoints msgpack-native and immune to that.

        `pickle_fallback` stays on as belt-and-braces for anything unexpected,
        but nothing in the designed state should need it; test_warehouse.py
        asserts that no checkpoint row is pickle-typed.
        """
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        saver = SqliteSaver(self.con, serde=JsonPlusSerializer(pickle_fallback=True))
        try:
            saver.setup()
        except Exception:
            pass
        return saver

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass


_STORE = None


def get_store(path: str = None) -> ChatStore:
    """Process-wide singleton — one SQLite handle, guarded by the module lock."""
    global _STORE
    if _STORE is None:
        _STORE = ChatStore(path)
    return _STORE
