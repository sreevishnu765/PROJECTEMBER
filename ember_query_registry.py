"""
ember_query_registry.py
=========================
Companion to ember_tools_registry.py, for a different job. That registry
answers "the user typed something that means DO X" (launch an app, create
a reminder). This one answers "a connected client wants to KNOW X right
now" (current system status, quota remaining, what's in memory) — a
frontend panel asking a structured question and expecting structured
data back, not a sentence to render as chat.

Why this needs to exist at all rather than reusing the chat path: every
one of these already has a natural-language way to get at it today
("Ember, status", "what do you remember about X") — see ember_core.py's
STATUS/INSPECT_MEMORY command handlers. That's the right interface for a
person typing in the chat box. It is the WRONG interface for a UI panel
that wants to render a list of memories as rows with per-row forget
buttons, or a status dashboard that refreshes without spawning a visible
chat turn every time it does — those need real data (a list of dicts,
numbers), not a pre-formatted "Status, sir:\n  Cloud (Gemini):
available\n..." string meant to be read by a person.

Same registry-not-if/elif reasoning ember_tools_registry.py already
documents: adding a new queryable thing (projects_list, files_list,
history_list, ...) means one handler function and one register() call
here, not a growing branch in ember_transport.py's message-type dispatch.

Confirmation gating: a query CAN be destructive (memory_forget deletes
rows) — same contract as ember_tools_registry.py's Tool.destructive,
reusing the exact same ember_confirmation gate rather than inventing a
second confirmation mechanism for queries specifically. Non-destructive
queries (the large majority — anything that only reads data) never touch
the gate at all.
"""

from dataclasses import dataclass
from typing import Callable, Optional

import ember_confirmation

# A handler receives the raw params dict from the client's {"type":
# "query", ...} message and returns a JSON-serializable dict (or list) —
# never raises for expected failure modes (bad/missing params), only for
# genuinely unexpected ones; dispatch() catches those as a last resort,
# same "handler owns its own expected errors" contract as
# ember_tools_registry.py's ToolHandler.
QueryHandler = Callable[[dict], "dict | list"]


@dataclass
class Query:
    name: str
    description: str
    handler: QueryHandler
    destructive: bool = False
    confirm_label: "str | None" = None  # human-friendly text for the confirmation dialog; defaults to `name` if unset


class QueryRegistry:
    def __init__(self):
        self._queries: "dict[str, Query]" = {}

    def register(self, name: str, description: str, handler: QueryHandler, destructive: bool = False, confirm_label: "str | None" = None) -> None:
        if name in self._queries:
            raise ValueError(f"Query {name!r} is already registered — pick a distinct name.")
        self._queries[name] = Query(name=name, description=description, handler=handler, destructive=destructive, confirm_label=confirm_label)

    def dispatch(self, name: str, params: dict, confirm_gate: "ember_confirmation.CLIConfirmationGate | None" = None, conversation=None) -> dict:
        """Runs a registered query's handler, gating on confirmation first
        if it's marked destructive — identical shape to
        ToolRegistry.dispatch(), reusing the same gate rather than a
        second confirmation path. Returns {"ok": True, "data": ...} or
        {"ok": False, "error": "..."} — never raises, so
        ember_transport.py's one place that calls this never needs its
        own try/except per query type."""
        query = self._queries.get(name)
        if query is None:
            return {"ok": False, "error": f"Unknown query: {name!r}"}

        if query.destructive:
            gate = confirm_gate or ember_confirmation.get_confirmation_gate()
            approved = gate.request_confirmation(query.confirm_label or query.name, params, conversation)
            if not approved:
                return {"ok": False, "error": f"'{name}' was not confirmed, so nothing was changed."}

        try:
            data = query.handler(params)
            return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": f"'{name}' failed unexpectedly: {e}"}

    def list_queries(self) -> "list[dict]":
        return [{"name": q.name, "description": q.description, "destructive": q.destructive} for q in self._queries.values()]


_registry_instance = None


def get_query_registry() -> QueryRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = QueryRegistry()
    return _registry_instance
