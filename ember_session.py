"""
ember_session.py
=================
Short-term conversation continuity across restarts — the missing piece
identified in the architecture review of ember_core.py's `_history`.

Where this sits relative to the two things Ember already had:

  - `_history` (ember_core.py)     in-process working context. Cheap,
                                    always available, gone the instant the
                                    process exits — the ONLY thing that
                                    changes below is that it's now also
                                    mirrored to disk as it happens.

  - `EmberMemory`  (ember_memory.py) durable, gated, embedding-searched
                                    FACTS individually extracted from
                                    conversation (extract_memory_candidate()),
                                    kept indefinitely, pruned only by
                                    relevance/decay — not by time or count.

  - `EmberSession` (here)          the same raw turns as `_history`,
                                    mirrored verbatim to disk, so a restart
                                    mid-conversation (crash, planned
                                    restart, laptop sleep/wake) doesn't
                                    silently wipe the last few exchanges.
                                    This is NOT a durable-facts store and
                                    NOT a transcript archive — see the
                                    bounds below.

Why this needed to be a third thing rather than reusing EmberMemory's
table: EmberMemory's rows are individually gated (most turns produce zero
rows — a greeting or a one-line answer has nothing "worth remembering"),
and its access pattern is similarity search, not ordered replay. Session
continuity needs EVERY turn, verbatim, in exact order, regardless of
whether anything in it looked fact-shaped — a different enough access
pattern that bolting it onto the same table would recreate exactly the
kind of layer-conflation problem already flagged and fixed in
ember_skills.py (durable rules vs. one-off personal facts mixed in one
file). Keeping this in its own table/module makes the boundary explicit
instead of implicit.

Explicit bounds, so this can't become the "enormous uncontrolled
transcript database" this was fixed to avoid:
  - SESSION_TURN_CAP: a hard global cap on stored rows (not per-session).
    Once hit, the oldest rows are deleted on every write — old sessions
    age out completely, there is no "browse conversation from 3 weeks
    ago" capability here, on purpose.
  - RESUME_WINDOW_MINUTES: a previous session's turns are only ever
    auto-loaded back into `_history` if that session's last activity was
    within this window. Beyond it, Ember starts a clean conversational
    slate (same as it always has) — an old, unrelated session is never
    silently re-injected into a new conversation just because rows still
    exist on disk.
  - No embedding, no relevance scoring, no search here at all — it's a
    plain ordered log. Anything needing semantic recall belongs in
    EmberMemory, not here; mixing the two access patterns is the exact
    mistake this design avoids.

Deferred, explicitly, not silently skipped:
  - Multi-session / named "project" resume (e.g. "pick back up on the
    thing we discussed about X yesterday" as a distinct, browsable,
    intentionally-resumed thread) is a real, larger feature. This module
    only solves the narrower "don't lose the last few minutes of context
    on an unplanned restart" problem — treat named-project resume as a
    separate, later, deliberately-scoped piece of work, not an accidental
    side effect of these rows existing on disk.
  - No command surfaces sessions outside the resume window today. Rows
    persist there (until the cap prunes them) but nothing currently lists
    or manually re-attaches an out-of-window session.
"""

import sqlite3
import threading
import time
import uuid
from pathlib import Path

RESUME_WINDOW_MINUTES = 120  # "still mid-conversation" cutoff — a first guess, tune on real usage same as every other heuristic constant in this project
SESSION_TURN_CAP = 200        # global cap across ALL sessions, not per-session — bounded by design, not a transcript archive


class EmberSession:
    """Thread-safe, SQLite/WAL-backed mirror of recent conversation turns,
    used only to survive an unplanned restart within the resume window.
    Never raises to the caller — every method degrades to "no crash-resume
    for this bit," never to a crashed turn or a crashed startup."""

    def __init__(self, db_path: "str | Path"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()
        self.session_id = uuid.uuid4().hex[:12]

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS session_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    used_search INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)

    def append(self, role: str, content: str, used_search: bool = False) -> None:
        """Mirror one turn to disk. A failed write here degrades to 'this
        turn won't survive a crash' — it must never take down the turn
        itself, which ember_core.py's in-memory _history already handled
        successfully by the time this is called."""
        now = time.time()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO session_turns (session_id, role, content, used_search, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.session_id, role, content, int(used_search), now),
                )
                self._prune_locked()
        except Exception as e:
            print(f"[ember_session] Warning: couldn't persist turn ({e}) — continuing without crash-resume for it.")

    def _prune_locked(self) -> None:
        """Caller already holds self._lock and is inside a `with self._conn`
        transaction block. Deletes everything past the global cap, oldest
        first — deliberately global, not per-session, so a long session
        can't let unrelated older sessions pile up unbounded either."""
        self._conn.execute(
            "DELETE FROM session_turns WHERE id NOT IN "
            "(SELECT id FROM session_turns ORDER BY id DESC LIMIT ?)",
            (SESSION_TURN_CAP,),
        )

    def load_resumable_history(self, max_turns: int) -> list:
        """Returns up to `max_turns` of the most recent PRIOR session's
        turns, oldest-first, in the same shape ember_core.py's `_history`
        already uses — ready to seed it directly. Returns [] (clean slate)
        if there's nothing stored, or if the most recent activity is older
        than RESUME_WINDOW_MINUTES. Never raises."""
        try:
            with self._lock:
                last_ts_row = self._conn.execute(
                    "SELECT MAX(created_at) FROM session_turns"
                ).fetchone()
                last_ts = last_ts_row[0] if last_ts_row else None
                if last_ts is None:
                    return []

                age_minutes = (time.time() - last_ts) / 60.0
                if age_minutes > RESUME_WINDOW_MINUTES:
                    return []

                rows = self._conn.execute(
                    "SELECT role, content, used_search FROM session_turns "
                    "ORDER BY id DESC LIMIT ?",
                    (max_turns,),
                ).fetchall()
            rows.reverse()
            return [{"role": r, "content": c, "used_search": bool(u)} for r, c, u in rows]
        except Exception as e:
            print(f"[ember_session] Warning: couldn't load resumable history ({e}) — starting fresh.")
            return []

    def clear(self) -> None:
        """Wipes the disk-mirrored short-term context entirely and starts
        a fresh session_id. Used by the CLEAR_CONTEXT / NEW_CONVERSATION
        intents — deliberately total, not selective, since this is
        disposable scratch data by design (unlike EmberMemory, which
        forget() treats as auditable and selective). Without this, an
        in-memory _history.clear() alone would still leave the old turns
        on disk, and a restart within the resume window would silently
        bring the "cleared" context right back."""
        try:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM session_turns")
        except Exception as e:
            print(f"[ember_session] Warning: couldn't clear persisted session turns ({e}).")
        self.session_id = uuid.uuid4().hex[:12]
