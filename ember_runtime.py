"""
ember_runtime.py
=================
Formalizes what ember_core.py's `run()` used to do ad hoc: start the
proactive engine thread, start the reminder poll thread, and (nothing
before this) wire a notification manager to the event bus. Per the
next-phase brief's item #9 ("Ember should not only exist while the user
is actively talking to her... build a proper background runtime").

This is deliberately a thin composition object, not a new execution
model — it owns references to the subsystems that already run their own
background threads (ProactiveEngine, ReminderStore, and now
GoogleAuthWatchdog) and gives them one start()/stop() so ember_core.py's
run() doesn't need to know the startup order or wiring details of each.
Keeping resource usage appropriate for a local machine means this stays
a handful of daemon threads, not a process pool or a scheduler service —
the brief is explicit that this should stay practical for one machine,
not distributed infrastructure.

google_auth_watchdog is Optional and defaults to None (unlike
proactive_engine/reminder_store, which are required) — it's a stopgap
tied to a specific, named trade-off (staying in Google's Testing
publishing status rather than pursuing full verification; see
google_auth.py's module docstring), not a core piece of Ember's
architecture. A caller that never configured Google auth at all
shouldn't need to construct a watchdog object just to satisfy this
dataclass.
"""

from dataclasses import dataclass
from typing import Optional

from ember_bus import EventBus, get_bus
from ember_notifications import NotificationManager
from ember_proactive import ProactiveEngine
from ember_reminders import ReminderStore
from ember_tools.google_auth_watchdog import GoogleAuthWatchdog


@dataclass
class EmberRuntime:
    proactive_engine: ProactiveEngine
    reminder_store: ReminderStore
    notification_manager: NotificationManager
    google_auth_watchdog: Optional[GoogleAuthWatchdog] = None
    bus: EventBus = None

    def __post_init__(self):
        if self.bus is None:
            self.bus = get_bus()
        self.notification_manager.attach_to_bus(self.bus)

    def start(self) -> None:
        self.proactive_engine.start()
        self.reminder_store.start()
        if self.google_auth_watchdog is not None:
            self.google_auth_watchdog.start()
        print("[ember_runtime] Background runtime started (proactive engine + reminder scheduler"
              + (" + Google auth watchdog" if self.google_auth_watchdog is not None else "") + ").")

    def stop(self) -> None:
        self.proactive_engine.stop()
        self.reminder_store.stop()
        if self.google_auth_watchdog is not None:
            self.google_auth_watchdog.stop()
        print("[ember_runtime] Background runtime stopped.")
