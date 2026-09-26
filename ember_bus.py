"""
ember_bus.py
============
Ember's event/action bus — the backbone connecting subsystems (memory,
tasks, tools, proactive engine, notifications) without each one importing
and directly calling into the others.

Deliberately NOT a distributed system, a message queue, or async — Ember
is a personal assistant running on one machine (per the explicit
"don't overengineer" instruction). This is a synchronous, in-process
observer pattern: `publish()` calls every subscribed handler immediately,
in the same thread, in registration order. That's the right amount of
machinery for what this actually needs to do: let a reminder firing
notify the notification manager without ember_reminders.py importing
ember_notifications.py directly, and let new subsystems (calendar,
weather, GitHub, ...) plug in by subscribing rather than by ember_core.py
growing another hardcoded call site every time.

Event naming convention: "domain.event", e.g. "reminder.due",
"memory.stored", "action.executed", "proactive.message". Not enforced by
code — just a convention so event names stay greppable and collision-free
as more subsystems are added.

Failure handling: one subscriber raising must never break the others, or
break the publisher (a reminder firing should still print/log even if a
notification-channel handler throws). Each handler is called inside its
own try/except; failures are printed with the event name and handler name
so a broken subscriber is loud in the logs without taking anything else
down — same graceful-degradation contract as the rest of Ember.
"""

import threading
from collections import defaultdict
from typing import Any, Callable

EventHandler = Callable[[str, Any], None]


class EventBus:
    """Thread-safe synchronous pub/sub. One process-wide instance is
    expected (see get_bus() below) — subsystems subscribe once at startup
    and publish whenever something relevant happens."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: "dict[str, list[EventHandler]]" = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        with self._lock:
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        with self._lock:
            handlers = self._subscribers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    def publish(self, event_type: str, payload: Any = None) -> None:
        """Calls every handler subscribed to `event_type`, synchronously,
        in the calling thread. A handler raising is caught and logged —
        it does not stop remaining handlers from running, and never
        propagates back to the publisher (publishing an event should
        never be able to crash whatever just happened)."""
        with self._lock:
            handlers = list(self._subscribers.get(event_type, []))
        for handler in handlers:
            try:
                handler(event_type, payload)
            except Exception as e:
                name = getattr(handler, "__name__", repr(handler))
                print(f"[ember_bus] subscriber {name!r} failed on {event_type!r}: {e}")

    def subscriber_count(self, event_type: str) -> int:
        with self._lock:
            return len(self._subscribers.get(event_type, []))


_bus_instance = None


def get_bus() -> EventBus:
    """Module-level singleton, same pattern as ember_confirmation.py's
    get_confirmation_gate() and ember_core.py's memory/session globals —
    one bus per process, everyone imports this to reach it rather than
    passing a bus instance around through every constructor."""
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = EventBus()
    return _bus_instance
