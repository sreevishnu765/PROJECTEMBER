"""
ember_notifications.py
=======================
Notification abstraction, per the next-phase brief: "don't hardcode
notifications into the task system... [it] should be another service/
capability that other subsystems can invoke."

Before this: ember_reminders.py's fired reminders and ember_proactive.py's
messages each called a hardcoded notify_fn (a plain print, injected at
construction time in ember_core.py). That already avoided the WORSE
version of this problem (jarvisforember hardcoded Telegram directly inside
the scheduler) but still meant every new delivery channel would need
threading a new notify_fn through two different constructors by hand.

This version: subsystems publish events onto the shared bus
("reminder.due", "proactive.message" — already wired in ember_reminders.py
and ember_core.py's proactive notify_fn respectively). NotificationManager
subscribes to both and fans out to whatever channels are registered.
Adding a channel (desktop toast, a future phone push, Telegram if you ever
want it back) means writing one function and appending it to `channels`
— no changes to ember_reminders.py, ember_proactive.py, or ember_core.py.

Channels today: just `console_channel` (prints, same visible behavior as
before this file existed). Voice output is an obvious near-term channel
once the voice pipeline exists — NOT wired here yet, per this session's
explicit scope (voice is out of scope for this pass) — but the seam is
exactly `channels.append(voice_channel)`, nothing structural needs to
change in this file when that's ready.
"""

from dataclasses import dataclass, field
from typing import Callable

NotificationChannel = Callable[[dict], None]


def console_channel(notification: dict) -> None:
    """Default channel — plain print, same visible output the previous
    hardcoded notify_fn lambdas produced, just routed through here now."""
    category = notification.get("category", "notice")
    message = notification.get("message", "")
    prefix = {
        "reminder": "Ember — reminder, sir",
        "proactive": "Ember — unprompted, sir",
        "google_auth": "Ember — heads up, sir",
    }.get(category, "Ember")
    print(f"\n[{prefix}]: {message}\n")


@dataclass
class NotificationManager:
    channels: "list[NotificationChannel]" = field(default_factory=lambda: [console_channel])

    def notify(self, message: str, category: str = "notice", **extra) -> None:
        """Fans out one notification to every registered channel. A
        failing channel is logged and skipped — one broken delivery path
        (e.g. a future network-based channel that's temporarily down)
        must never suppress delivery on the others, same
        graceful-degradation contract as everything else here."""
        notification = {"message": message, "category": category, **extra}
        for channel in self.channels:
            try:
                channel(notification)
            except Exception as e:
                name = getattr(channel, "__name__", repr(channel))
                print(f"[ember_notifications] channel {name!r} failed: {e}")

    def attach_to_bus(self, bus) -> None:
        """Subscribes this manager to the events it knows how to turn
        into user-facing notifications. Kept as an explicit opt-in method
        (rather than auto-subscribing in __init__) so tests/tools can
        construct a NotificationManager without it silently attaching to
        the process-wide bus — same "explicit over implicit" reasoning as
        everywhere else in this project's wiring."""
        bus.subscribe("reminder.due", self._on_reminder_due)
        bus.subscribe("proactive.message", self._on_proactive_message)
        bus.subscribe("google_auth.expiring_soon", self._on_google_auth_expiring)

    def _on_reminder_due(self, event_type: str, payload: dict) -> None:
        self.notify(payload.get("message", ""), category="reminder", reminder_id=payload.get("id"), priority=payload.get("priority"))

    def _on_proactive_message(self, event_type: str, payload: dict) -> None:
        message = payload.get("message", payload) if isinstance(payload, dict) else payload
        self.notify(message, category="proactive")

    def _on_google_auth_expiring(self, event_type: str, payload: dict) -> None:
        age = payload.get("age_days", "several")
        message = (
            f"Your Google authorization is about {age} day(s) old — Testing-mode grants "
            "lapse around 7 days after consent. Trigger a calendar or Drive command soon "
            "so I can prompt a fresh browser sign-in before it actually expires."
        )
        self.notify(message, category="google_auth")
