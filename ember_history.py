"""
ember_history.py
==================
Real, browsable conversation history — the piece ember_session.py
explicitly does NOT provide, by design (see that module's own docstring:
"NOT a transcript archive", bounded by a global 200-row cap, resume-only,
"Deferred, explicitly, not silently skipped: ... a real, larger feature").
This is that deferred, larger feature, built as its own module rather
than by loosening ember_session's cap.

Why a separate module instead of just raising SESSION_TURN_CAP: crash-
resume and browsable history are genuinely different access patterns —
"survive an unplanned restart in the next two hours" wants small,
aggressively pruned, always-fresh; "let me find and reopen that
conversation from three weeks ago" wants kept-around and queryable by
session. Mixing them back into one table would recreate exactly the
layer-conflation problem this project already caught and fixed twice
elsewhere (ember_skills.py's rules/facts split; EmberMemory vs
ember_session.py's own split, spelled out in ember_session.py's
docstring). Two access patterns, two modules — same principle, applied
again.

Still bounded, not unlimited: HISTORY_TURN_CAP is a much larger ceiling
than ember_session's, but it IS a ceiling, pruned globally the same way,
for the same reason — this is a personal assistant on one machine, not
infrastructure that needs a real retention policy, and "no cap at all"
is how a SQLite file quietly becomes a problem eighteen months from now.
"""

import sqlite3
import threading
import time
from pathlib import Path

HISTORY_TURN_CAP = 20_000  # generous, not unlimited — see module docstring
PREVIEW_CHARS = 120


class ConversationHistory:
    """Thread-safe, SQLite/WAL-backed archive of every turn across every
    conversation. Populated by ember_core.py's _append_history() wrapper
    (see that function) rather than by EmberConversation itself — keeps
    ember_conversation.py's own contract (ephemeral, per-connection,
    durable state lives elsewhere) unchanged; this is durable state,
    orchestrated from the same place _append_history already writes to
    ember_session.py's crash-resume mirror."""

    def __init__(self, db_path: "str | Path"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    used_search INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_history_session ON history_turns(session_id)")

    def append(self, session_id: str, role: str, content: str, used_search: bool = False) -> None:
        """Never raises — a failed archive write degrades to 'this turn
        won't show up in Past Conversations', never to a crashed turn,
        same graceful-degradation contract as ember_session.py's own
        append()."""
        now = time.time()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO history_turns (session_id, role, content, used_search, created_at) VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, int(used_search), now),
                )
                self._prune_locked()
        except Exception as e:
            print(f"[ember_history] Warning: couldn't archive turn ({e}) — continuing without it in Past Conversations.")

    def _prune_locked(self) -> None:
        """Caller already holds self._lock and is inside a `with
        self._conn` transaction — same pattern as ember_session.py's own
        _prune_locked(). Global cap, not per-session, so one very long-
        running conversation can't crowd out every other session's
        history either."""
        self._conn.execute(
            "DELETE FROM history_turns WHERE id NOT IN "
            "(SELECT id FROM history_turns ORDER BY id DESC LIMIT ?)",
            (HISTORY_TURN_CAP,),
        )

    def list_sessions(self, limit: int = 50) -> "list[dict]":
        """Newest-active-first list of distinct sessions, each with its
        turn count, first/last timestamps, and a short preview (its
        first user turn, truncated) — enough for a Past Conversations
        list row without fetching every turn of every session up front."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT session_id, COUNT(*), MIN(created_at), MAX(created_at)
                    FROM history_turns
                    GROUP BY session_id
                    ORDER BY MAX(created_at) DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

                previews: "dict[str, str]" = {}
                for session_id, *_rest in rows:
                    preview_row = self._conn.execute(
                        "SELECT content FROM history_turns WHERE session_id = ? AND role = 'user' ORDER BY id ASC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    previews[session_id] = preview_row[0] if preview_row else ""
        except Exception as e:
            print(f"[ember_history] Warning: couldn't list sessions ({e}).")
            return []

        results = []
        for session_id, count, started, last in rows:
            preview = previews.get(session_id, "")
            if len(preview) > PREVIEW_CHARS:
                preview = preview[:PREVIEW_CHARS] + "…"
            results.append(
                {
                    "session_id": session_id,
                    "turn_count": count,
                    "started_at": started,
                    "last_active_at": last,
                    "preview": preview,
                }
            )
        return results

    def get_session(self, session_id: str) -> "list[dict]":
        """Every turn for one session, oldest first — the actual
        transcript for reopening/browsing. Never raises; an unknown
        session_id just returns an empty list, same as a genuinely empty
        one."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT role, content, used_search, created_at FROM history_turns WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
        except Exception as e:
            print(f"[ember_history] Warning: couldn't load session {session_id!r} ({e}).")
            return []
        return [{"role": r, "content": c, "used_search": bool(u), "created_at": t} for r, c, u, t in rows]
