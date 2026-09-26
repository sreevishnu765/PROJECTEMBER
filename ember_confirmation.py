"""
ember_confirmation.py
======================
Confirmation gate for destructive tool calls, adapted from jarvisforember's
server.py pattern (threading.Event bridged to a WebSocket broadcast) down to
a CLI-native equivalent, since Ember has no frontend yet.

Deliberately excludes the "jarvis is freaky" bypass phrase found in the
original — that was a real prompt-injection backdoor (any text Ember reads,
including untrusted content, could contain that phrase and silently approve
a pending destructive action). There is no bypass here. Confirmation can only
come from a real blocking input() call in the same process that owns the
terminal, never from model-generated or fetched text.

Contract (matches jarvis_local.py's self.on_confirm_request exactly, so it
drops into the same call sites unchanged):

    approved: bool = confirm_gate.request_confirmation(tool_name, args)

Swap-out path for later: once Ember has a WebSocket/HUD frontend, replace
CLIConfirmationGate with a WebSocket-backed one that implements the same
request_confirmation(tool_name, args) -> bool method. Nothing that calls
this needs to change.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


# Tool name patterns that require confirmation before execution.
# Match by substring on the tool/function name — keep this list explicit
# and reviewed; do not make it regex-clever, clarity here matters more
# than brevity.
DESTRUCTIVE_TOOL_PATTERNS = (
    "delete",
    "remove",
    "clear_all",
    "wipe",
    "send_email",
    "send_message",
    "purge",
    "overwrite",
    "format",
    "shutdown",
    "uninstall",
)


def is_destructive(tool_name: str) -> bool:
    name = (tool_name or "").lower()
    return any(pattern in name for pattern in DESTRUCTIVE_TOOL_PATTERNS)


@dataclass
class CLIConfirmationGate:
    """
    Thread-safe confirmation gate for a single-terminal CLI assistant.

    Usage from your tool-execution loop (wherever ember_core.py currently
    calls a tool function directly):

        if ember_confirmation.is_destructive(fn_name):
            approved = confirm_gate.request_confirmation(fn_name, args)
            if not approved:
                result = f"Action '{fn_name}' was not confirmed by the user."
                # skip execution, return result to the model
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)

    def request_confirmation(self, tool_name: str, args: dict, conversation=None) -> bool:
        """
        Blocks the calling thread (safe even if called from a worker thread
        spawned per-message) until the user types y/n at the terminal.
        No timeout auto-approve — an unanswered confirmation must default
        to "not approved", never to "approved". A stuck terminal should
        fail closed, not open.

        `conversation` is accepted but unused — the CLI has exactly one
        terminal regardless of which EmberConversation asked (in practice
        the CLI only ever has one, "cli-main"). It's here purely so this
        class satisfies the same call signature as SessionConfirmationGate
        below — ember_tools_registry.py's dispatch() can call either
        without knowing which one it has.
        """
        with self._lock:
            print("\n[CONFIRMATION REQUIRED]")
            print(f"  Tool: {tool_name}")
            print(f"  Args: {args}")
            while True:
                try:
                    resp = input("  Approve this action? [y/N]: ").strip().lower()
                except EOFError:
                    # No interactive input available (e.g. running headless).
                    # Fail closed.
                    print("  No interactive input available — denying by default.")
                    return False
                if resp in ("y", "yes"):
                    return True
                if resp in ("", "n", "no"):
                    return False
                print("  Please answer y or n.")


@dataclass
class PendingConfirmation:
    request_id: str
    tool_name: str
    args: dict
    session_id: str
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class SessionConfirmationGate:
    """
    Confirmation gate for non-CLI conversations (a future WebSocket/voice
    client) — added this session as part of the pre-transport-layer work.
    CLIConfirmationGate's request_confirmation() blocks on input(), which
    has no meaning for a remote client; this gate publishes a
    "confirmation.requested" event on the shared bus instead and blocks
    on a per-request threading.Event, which a transport handler resolves
    later by calling resolve() with the client's actual answer.

    Fail-closed on timeout, not just on missing input: a CLI terminal is
    always "there" (EOFError is the only failure mode, handled above by
    denying). A remote client can disconnect mid-request and simply never
    answer — an indefinite block here would leak a thread per abandoned
    confirmation forever. DEFAULT_TIMEOUT_SECONDS bounds that; an
    unanswered request past the timeout is treated as "not approved,"
    same fail-closed contract as the CLI gate, never approved by default,
    never approved silently.
    """

    DEFAULT_TIMEOUT_SECONDS = 120

    def __init__(self, bus=None):
        self._bus = bus
        self._pending: "dict[str, PendingConfirmation]" = {}
        self._lock = threading.Lock()

    def request_confirmation(self, tool_name: str, args: dict, conversation=None, timeout: "float | None" = None) -> bool:
        request_id = uuid.uuid4().hex[:12]
        session_id = conversation.session_id if conversation is not None else "unknown"
        pending = PendingConfirmation(request_id=request_id, tool_name=tool_name, args=args, session_id=session_id)
        with self._lock:
            self._pending[request_id] = pending

        if self._bus is not None:
            self._bus.publish("confirmation.requested", {
                "request_id": request_id,
                "tool_name": tool_name,
                "args": args,
                "session_id": session_id,
            })
        else:
            # No bus wired up at all — there is genuinely no way to reach
            # a remote client, so fail closed immediately rather than
            # waiting out a full timeout for a request nobody could ever
            # have seen.
            with self._lock:
                self._pending.pop(request_id, None)
            return False

        answered = pending.event.wait(timeout=timeout if timeout is not None else self.DEFAULT_TIMEOUT_SECONDS)
        with self._lock:
            self._pending.pop(request_id, None)

        if not answered:
            return False  # timed out — fail closed, same contract as every other gate here
        return pending.approved

    def resolve(self, request_id: str, approved: bool) -> bool:
        """Called by a transport handler when the remote client answers.
        Returns True if a matching pending request was found and
        resolved, False if request_id is unknown (already resolved,
        already timed out, or never existed) — callers should treat a
        False return as an expected race (a late/duplicate answer
        arriving after timeout), not an error."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        pending.approved = approved
        pending.event.set()
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


# Module-level singleton, mirroring the get_memory()-style pattern already
# used elsewhere in Ember for shared state.
_gate_instance = None


def get_confirmation_gate() -> CLIConfirmationGate:
    global _gate_instance
    if _gate_instance is None:
        _gate_instance = CLIConfirmationGate()
    return _gate_instance


_session_gate_instance = None


def get_session_confirmation_gate(bus=None) -> SessionConfirmationGate:
    """Separate singleton from get_confirmation_gate() — a transport
    layer's conversations use this one, the CLI's default conversation
    keeps using the CLI gate. bus is only used on first creation; pass
    ember_bus.get_bus() from the caller that first wires this up."""
    global _session_gate_instance
    if _session_gate_instance is None:
        _session_gate_instance = SessionConfirmationGate(bus=bus)
    return _session_gate_instance