"""
ember_conversation.py
=======================
Per-conversation session state, split out of ember_core.py's module-level
globals (_history, _session) — the actual missing piece before a
transport layer or multi-device connectivity can mean anything.

Before this: _history was a single, process-wide list and _session was a
single EmberSession instance. That works for exactly one CLI user typing
into one terminal. It has no way to represent "the CLI and a WebSocket
client are both talking to Ember right now" without one silently
clobbering the other's history — this isn't a thread-safety bug to patch
with a lock, it's a missing layer: nothing about the OLD design lets two
conversations coexist at all, locked or not.

What stays global/shared, deliberately — per the multi-device brief's own
distinction ("potentially synchronized: tasks, reminders, memories,
conversations..." — memories/reminders are the shared BACKEND;
conversations are per-device): EmberMemory, ReminderStore, the tool
registry, the event bus, the confirmation-gate infrastructure. All of
those stay singletons in ember_core.py, unchanged. Only the ephemeral,
per-conversation pieces move here: turn history, the disk-mirrored
crash-resume log (one ember_session.EmberSession PER conversation now,
not one globally — so two conversations' turns don't interleave in the
same session_turns rows), and a cancellation flag.

Cancellation is a plain threading.Event, not asyncio — Ember's provider
calls (llm_client.py) are synchronous HTTP calls today, so a background
thread checking this event between chunks (once streaming exists) or
between bounded steps (e.g. ember_research.py's search rounds) is the
right primitive, not an asyncio-only mechanism that would need a bigger
rewrite to even reach the code that needs to check it.
"""

import threading
import uuid
from pathlib import Path

from ember_session import EmberSession

HISTORY_MAX_TURNS = 6  # kept in sync with ember_core.py's own constant of the same name


class EmberConversation:
    """One isolated conversation's ephemeral state. Durable state
    (memory, reminders) is NOT here — conversations read/write those
    through the shared singletons in ember_core.py, same as before."""

    def __init__(self, session_id: "str | None" = None, session_db_path: "str | Path | None" = None, confirm_gate=None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.history: list = []
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        # One disk-mirror per conversation, not global — a WebSocket
        # client's crash-resume history must never bleed into the CLI's,
        # or vice versa. None is valid (e.g. a short-lived transport
        # session that doesn't need crash-resume at all).
        self.disk_session = EmberSession(session_db_path) if session_db_path else None
        # Which confirmation gate this conversation's destructive tool
        # calls should go through. None means "use the CLI's default
        # gate" (ember_core.py resolves that) — a future transport-created
        # conversation passes its own SessionConfirmationGate here instead,
        # since a remote client has no terminal for CLIConfirmationGate's
        # blocking input() to prompt on.
        self.confirm_gate = confirm_gate

    def append(self, role: str, content: str, used_search: bool = False) -> None:
        with self._lock:
            self.history.append({"role": role, "content": content, "used_search": used_search})
            del self.history[:-HISTORY_MAX_TURNS]
        if self.disk_session:
            self.disk_session.append(role, content, used_search)

    def snapshot_history(self) -> list:
        """Returns a shallow copy — callers (llm_client.generate's history
        argument, classify_intent's follow-up heuristics) should never
        hold a live reference to the internal list, since another thread
        could be appending to it concurrently for a different in-flight
        turn on the SAME conversation (e.g. a queued follow-up arriving
        before the first reply finishes)."""
        with self._lock:
            return list(self.history)

    def load_resumable_history(self, max_turns: int) -> list:
        if not self.disk_session:
            return []
        return self.disk_session.load_resumable_history(max_turns)

    def seed_history(self, turns: list) -> None:
        with self._lock:
            self.history.extend(turns)
            del self.history[:-HISTORY_MAX_TURNS]

    def clear(self) -> None:
        """CLEAR_CONTEXT / NEW_CONVERSATION — wipes THIS conversation's
        history and disk mirror only. Other concurrent conversations (a
        different device/session) are untouched, and EmberMemory's
        durable facts are never touched here either."""
        with self._lock:
            self.history.clear()
        if self.disk_session:
            self.disk_session.clear()

    def request_cancel(self) -> None:
        """Sets the cancel flag. Takes effect wherever code actually
        checks is_cancelled() — today that's at the start of a new turn
        and between ember_research.py's bounded search rounds; real
        mid-generation cancellation additionally takes effect inside
        llm_client.generate_stream()'s chunk loop once a caller uses the
        streaming path. A non-streaming llm_client.generate() call is a
        single blocking HTTP request with nothing to check mid-flight —
        setting this flag during one will not interrupt it; it will be
        honored at the next checkpoint."""
        self.cancel_event.set()

    def reset_cancel(self) -> None:
        """Called at the start of each new turn — a cancel flag left set
        from a turn that already finished must not spuriously cancel the
        NEXT one."""
        self.cancel_event.clear()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()


class ConversationRegistry:
    """Creates/retrieves EmberConversation objects by session_id. One
    process-wide instance (get_conversation_registry()), same singleton
    pattern as the event bus, tool registry, etc."""

    def __init__(self):
        self._conversations: "dict[str, EmberConversation]" = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str, session_db_path: "str | Path | None" = None) -> EmberConversation:
        with self._lock:
            if session_id not in self._conversations:
                self._conversations[session_id] = EmberConversation(session_id, session_db_path)
            return self._conversations[session_id]

    def get(self, session_id: str) -> "EmberConversation | None":
        with self._lock:
            return self._conversations.get(session_id)

    def close(self, session_id: str) -> None:
        """Drops a conversation entirely — used when a transport client
        disconnects. Does NOT delete its disk-mirrored session_turns rows
        (those age out via ember_session.py's own global cap/resume-window
        logic on their own schedule); this only stops holding it in memory."""
        with self._lock:
            self._conversations.pop(session_id, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._conversations)


_registry_instance = None


def get_conversation_registry() -> ConversationRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ConversationRegistry()
    return _registry_instance
