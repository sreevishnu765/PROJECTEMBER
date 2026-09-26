"""
ember_tools_registry.py
========================
Ember's unified capability layer — priority #1 from the post-Jarvis-port
development phase.

Before this: ember_core.py's `_classify_action`/`_dispatch_action` was a
hand-written if/elif chain, one branch per capability (clear_memory,
create_reminder, list_reminders, cancel_reminder, launch_app,
generate_pdf, analyze_document). That worked at 7 capabilities. It does
NOT scale to calendar/weather/Spotify/email/GitHub/computer-control/etc
without ember_core.py growing indefinitely and becoming exactly the
"5,000-line god file" explicitly flagged as a failure mode to avoid.

This registry is the fix: every capability is a `Tool` — name,
description, a regex (or callable) that decides whether a message
invokes it, a handler, and a `destructive` flag. Adding a new capability
means writing a handler function and calling `registry.register(...)`
once — ember_core.py's main loop and intent classifier never need to
change.

What this deliberately is NOT:
  - Not LLM-driven tool selection. The trigger-matching stays regex/
    heuristic, same reasoning ember_core.py already documented for its
    action layer: the vocabulary is enumerable and imperative-shaped,
    unlike the genuinely open-ended "does this need a web search"
    problem that legitimately needed native tool-calling. If/when the
    capability count and phrasing variety grows past what regex can
    reasonably cover, an LLM-driven selection layer can be added ON TOP
    of this registry (the registry itself doesn't have to change — it
    would just gain a second way to be reached) — not a reason to avoid
    building the registry now.
  - Not a plugin-loading system (dynamically importing third-party code
    from a directory). Every tool is still a plain Python function
    registered in this process, per the "don't overengineer a personal
    assistant on one machine" instruction. A load-skills-from-disk-style
    dynamic loader is a distinct, separately-scoped feature if ever
    needed.

Confirmation gating lives HERE, once, instead of being hand-wired into
each dispatch branch: `dispatch()` checks `tool.destructive` and routes
through ember_confirmation before calling the handler, so a future
destructive tool (delete_file, send_email, ...) gets the safety net for
free just by setting destructive=True — not by remembering to copy the
gating code into a new branch.
"""

import re
from dataclasses import dataclass
from typing import Callable, Optional

import ember_confirmation

# A handler receives the raw regex match (so it can pull out captured
# groups) and returns the reply text. It must never raise for expected
# failure modes (bad args, missing file, etc.) — same contract as every
# other user-facing function in this project; dispatch() catches
# unexpected exceptions as a last resort, but a handler that relies on
# that instead of its own error handling is a bug in the handler, not a
# feature of the registry.
ToolHandler = Callable[["re.Match"], str]


@dataclass
class Tool:
    name: str
    description: str          # short, human-readable — used in list_tools() / future LLM-driven selection
    pattern: "re.Pattern"      # compiled regex; dispatch() tries these in registration order
    handler: ToolHandler
    destructive: bool = False  # if True, routes through ember_confirmation before handler runs


class ToolRegistry:
    def __init__(self):
        self._tools: "list[Tool]" = []

    def register(
        self,
        name: str,
        description: str,
        pattern: str,
        handler: ToolHandler,
        destructive: bool = False,
        flags: int = re.IGNORECASE,
    ) -> None:
        """Registers a tool. `pattern` is compiled once here — callers
        pass a raw regex string, not a pre-compiled Pattern, so tool
        definitions read as plain declarations (see
        ember_core.py's _register_builtin_tools for the full list)."""
        compiled = re.compile(pattern, flags)
        self._tools.append(Tool(name=name, description=description, pattern=compiled, handler=handler, destructive=destructive))

    def match(self, message: str) -> "tuple[Tool, re.Match] | None":
        """Returns the first registered tool whose pattern matches
        `message`, plus the match object, or None if nothing matches.
        Registration order is the priority order — same as the original
        _classify_action's top-to-bottom if/elif, preserved deliberately
        so existing behavior (e.g. clear_memory checked before the more
        general reminder patterns) doesn't silently change just because
        this got refactored."""
        for tool in self._tools:
            m = tool.pattern.search(message)
            if m:
                return tool, m
        return None

    def dispatch(self, tool: Tool, match: "re.Match", confirm_gate: "ember_confirmation.CLIConfirmationGate | None" = None, conversation=None) -> str:
        """Runs a matched tool's handler, gating on confirmation first if
        the tool is marked destructive. confirm_gate defaults to the
        process-wide CLI singleton if not given — a caller with a
        transport-created conversation should pass that conversation's
        own gate (see EmberConversation.confirm_gate) instead, so a
        remote client gets its confirmation prompt delivered over its own
        connection rather than blocking on the CLI's terminal input().
        conversation is passed through to the gate for the same reason
        (SessionConfirmationGate needs it to tag which client the pending
        request belongs to) — CLIConfirmationGate accepts and ignores it.
        Never raises — a handler that throws unexpectedly is caught here
        and turned into a plain-language failure message, same graceful-
        degradation contract as everything else, so a bug in one tool
        can't take down the whole turn."""
        if tool.destructive:
            gate = confirm_gate or ember_confirmation.get_confirmation_gate()
            args_preview = {"matched_text": match.group(0)}
            approved = gate.request_confirmation(tool.name, args_preview, conversation)
            if not approved:
                return f"Understood, sir — '{tool.name}' was not confirmed, so nothing was done."
        try:
            return tool.handler(match)
        except Exception as e:
            return f"'{tool.name}' failed unexpectedly, sir: {e}"

    def list_tools(self) -> "list[dict]":
        """For diagnostics / a future 'what can you do' command / a future
        LLM-driven selection layer that wants the capability list as
        structured data rather than reading this file."""
        return [{"name": t.name, "description": t.description, "destructive": t.destructive} for t in self._tools]


_registry_instance = None


def get_registry() -> ToolRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ToolRegistry()
    return _registry_instance
