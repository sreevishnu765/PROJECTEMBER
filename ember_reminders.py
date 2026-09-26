"""
ember_reminders.py
====================
Task/reminder system — the one branch of the original architecture diagram
("what are our tasks for today?" vs "what happened in the news today?" vs
"remind me to do laundry tonight") that was a placeholder until now.
ember_core.py's _ACTION_OVERRIDE_PATTERNS used to just prevent these
phrases from being misclassified as a web search; nothing was ever stored
or delivered. This module is the actual implementation.

Storage/scheduling architecture adapted from jarvisforember's
api/telegram_reminders.py — real, working code (JSON file, background poll
loop, add/list/cancel). Two concrete bugs fixed from the original:

  1. cancel_telegram_reminder(index) cancels by list position in a list
     that gets re-sorted after every add. If a reminder fires (removed by
     the scheduler thread) between the user viewing the list and issuing
     a cancel, every index after it silently shifts — the user could
     cancel the wrong reminder with no error at all. Same bug class
     already fixed in ember_memory.py (stable integer IDs, not
     index-based deletion) and flagged in vector_memory.py's audit. Fixed
     the same way here with a stable short ID per reminder.

  2. Hardcoded to Telegram delivery (send_telegram_message). Ember has no
     Telegram integration and no frontend yet. Delivery is now an injected
     notify_fn, same swap-out contract as ember_proactive.py's
     CLI-print-for-now / real-channel-later pattern.

  3. (Minor, not a correctness bug) The original polled every 1 second —
     excessive for something with minute-level granularity. Defaulted to
     20s here; 50x fewer wakeups for no perceptible difference in when a
     reminder actually fires.

Also NEW here, not in the original: natural-language time parsing.
jarvisforember's scheduler only accepted a strict 'YYYY-MM-DD HH:MM'
machine format — the actual natural-language interpretation ("tonight",
"in 2 hours") must have happened upstream via an LLM call in
jarvis_local.py's own tool-argument extraction, which Ember's action
layer deliberately doesn't use for its small, enumerable action vocabulary
(see the proportionality note in ember_core.py's action-intent section).
A regex/heuristic time parser is built below instead — covers the common
phrasings, fails honestly and explicitly on anything more exotic, same
graceful-degradation contract as every other heuristic in this project.
"""

import os
import re
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

DEFAULT_POLL_SECONDS = 20

_TIME_OF_DAY_DEFAULTS = {
    "tonight": (20, 0),
    "this evening": (19, 0),
    "evening": (19, 0),
    "this morning": (9, 0),
    "morning": (9, 0),
    "this afternoon": (14, 0),
    "afternoon": (14, 0),
    "noon": (12, 0),
    "midnight": (0, 0),
}

# Ordered longest-phrase-first so "this evening" matches before the bare
# "evening" substring inside it would.
_TIME_OF_DAY_KEYS_ORDERED = sorted(_TIME_OF_DAY_DEFAULTS.keys(), key=len, reverse=True)

TIME_PHRASE_RE = re.compile(
    r"\b(in\s+\d+\s*(?:minute|min|hour|hr)s?"
    # Day-word phrases (tomorrow/tonight/this evening/...) can optionally
    # be followed by a specific clock time — "tomorrow at 6 PM", "tonight
    # at 9" — and both pieces MUST be captured as one match. Before this
    # fix, "tomorrow" (leftmost) and "at 6 PM" (later in the string)
    # would each be separate potential matches, and re.search only ever
    # returns the single leftmost one — so "remind me to submit Alpha
    # tomorrow at 6 PM" only ever extracted "tomorrow", silently dropping
    # "at 6 PM" and defaulting to 9am. Confirmed via the exact reminder
    # example from the next-phase brief itself, not a hypothetical.
    r"|(?:tomorrow(?:\s+(?:morning|afternoon|evening))?"
    r"|tonight|this\s+evening|this\s+morning|this\s+afternoon"
    r"|evening|morning|afternoon|noon|midnight)"
    r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
    r"|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)?"
    r"|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\b",
    re.IGNORECASE,
)

# ---- Recurrence -----------------------------------------------------------
# Real gap flagged directly in the next-phase brief: "remind me every Sunday
# to update the model" had no path at all before this — TIME_PHRASE_RE above
# only ever recognized a single fire time, with no notion of "and repeat."
# Same honest-failure contract as parse_natural_time: covers the common
# phrasings (daily/weekly/named-weekday), fails by simply returning None
# (treated as "not recurring," not an error) on anything more exotic
# (monthly, "every other week", etc.) rather than guessing.
_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

RECURRENCE_PHRASE_RE = re.compile(
    r"\bevery\s+(day|" + "|".join(_WEEKDAY_NAMES) + r"|week)\b",
    re.IGNORECASE,
)


def parse_recurrence(phrase: str) -> "str | None":
    """Returns a normalized recurrence key ("daily" or "weekly:<0-6, Mon=0>"),
    or None if `phrase` doesn't contain recognizable recurrence language.
    Does not consume/strip the matched text — same division of labor as
    parse_natural_time: callers strip out what they've already parsed."""
    m = RECURRENCE_PHRASE_RE.search(phrase)
    if not m:
        return None
    unit = m.group(1).lower()
    if unit == "day":
        return "daily"
    if unit == "week":
        return f"weekly:{datetime.now().weekday()}"  # "every week" with no named day = same weekday as when it was set
    return f"weekly:{_WEEKDAY_NAMES.index(unit)}"


def next_occurrence(current_fire_at: datetime, recurrence: str) -> datetime:
    """Given a reminder's just-fired fire_at and its recurrence key, returns
    the next fire_at. Preserves the original time-of-day; only the date
    advances — "remind me every day at 8am" should keep firing at 8am, not
    drift to whatever time the poll loop happened to notice it was due."""
    if recurrence == "daily":
        return current_fire_at + timedelta(days=1)
    if recurrence.startswith("weekly:"):
        target_weekday = int(recurrence.split(":", 1)[1])
        days_ahead = (target_weekday - current_fire_at.weekday()) % 7
        days_ahead = days_ahead or 7  # always advance to the FOLLOWING occurrence, never the same day
        return current_fire_at + timedelta(days=days_ahead)
    # Unrecognized recurrence key (shouldn't happen via parse_recurrence,
    # but a corrupted/hand-edited JSON file could produce one) — treat as
    # one-shot rather than raising, same fail-safe-not-fail-crash contract
    # as the rest of this module.
    return current_fire_at


def parse_natural_time(phrase: str, now: Optional[datetime] = None) -> "tuple[Optional[datetime], str]":
    """
    Best-effort natural-language time parser. Returns (datetime, "") on
    success, or (None, error_message) on failure — never raises, matches
    the honest-failure contract used everywhere else in this project.

    Covers: "in N minutes/hours", "at H(:MM)(am/pm)", bare "H:MM(am/pm)",
    "tonight" / "tomorrow" / "this morning/afternoon/evening" / "noon" /
    "midnight", explicit 'YYYY-MM-DD HH:MM', and combinations like
    "tomorrow at 5pm". Anything else (relative weekdays, "next Tuesday",
    etc.) is out of scope for this pass — returns an honest failure rather
    than a wrong guess.
    """
    now = now or datetime.now()
    original = phrase.strip()
    phrase = original.lower()

    try:
        return datetime.strptime(phrase, "%Y-%m-%d %H:%M"), ""
    except ValueError:
        pass

    m = re.search(r"\bin\s+(\d+)\s*(minute|min|hour|hr)s?\b", phrase)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(hours=n) if unit.startswith("hour") or unit == "hr" else timedelta(minutes=n)
        return now + delta, ""

    is_day_after_tomorrow = "day after tomorrow" in phrase
    # Real bug: "day after tomorrow" contains the substring "tomorrow",
    # so the plain tomorrow-check below used to fire for it too — off by
    # one day, always. Checked first so the more specific phrase wins.
    is_tomorrow = (not is_day_after_tomorrow) and "tomorrow" in phrase

    # Bare weekday names ("wednesday", "reschedule to friday") were
    # explicitly out of scope before — the module docstring even said so.
    # Added this pass because the new calendar reschedule-by-name feature
    # needs it directly: "reschedule the team meeting to wednesday" is a
    # completely natural way to ask, and failing on it would make that
    # feature far less useful than intended. Reuses the same _WEEKDAY_NAMES
    # list already defined above for recurrence parsing. If today already
    # IS the named day, this means the NEXT occurrence (a week out), not
    # today — "reschedule to wednesday" said on a Wednesday shouldn't
    # silently mean "later today."
    weekday_index = next((i for i, wd in enumerate(_WEEKDAY_NAMES) if re.search(rf"\b{wd}\b", phrase)), None)

    if weekday_index is not None:
        days_ahead = (weekday_index - now.weekday()) % 7
        days_ahead = days_ahead or 7
        base_day = now + timedelta(days=days_ahead)
        is_explicit_future_day = True
    elif is_day_after_tomorrow:
        base_day = now + timedelta(days=2)
        is_explicit_future_day = True
    elif is_tomorrow:
        base_day = now + timedelta(days=1)
        is_explicit_future_day = True
    else:
        base_day = now
        is_explicit_future_day = False

    m = (
        re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", phrase)
        or re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", phrase)
        # Real, reproduced bug: a bare "4pm" with neither a preceding "at"
        # nor a colon matched NEITHER pattern above at all — this is the
        # actual root cause behind a calendar event silently defaulting
        # to 9am when a caller's own regex had already stripped the "at"
        # before this function ever saw the phrase (fixed separately in
        # ember_core.py's _CALENDAR_ADD_RE), but fixing it here too means
        # any other caller passing a bare "4pm"/"9am" (no "at", no colon)
        # works correctly regardless of how the phrase reached this
        # function. The empty group keeps this alternative's group
        # numbering aligned with the two above it (hour, minute, meridiem).
        or re.search(r"\b(\d{1,2})()\s*(am|pm)\b", phrase)
    )
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        meridiem = m.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None, f"'{original}' doesn't look like a valid time, sir."
        candidate = base_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now and not is_explicit_future_day:
            candidate += timedelta(days=1)  # "at 5pm" said at 6pm means tomorrow, not the past
        return candidate, ""

    for key in _TIME_OF_DAY_KEYS_ORDERED:
        if key in phrase:
            hour, minute = _TIME_OF_DAY_DEFAULTS[key]
            candidate = base_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now and not is_explicit_future_day:
                candidate += timedelta(days=1)
            return candidate, ""

    if is_explicit_future_day:
        return base_day.replace(hour=9, minute=0, second=0, microsecond=0), ""

    return None, (
        f"I couldn't figure out a time from '{original}', sir — try something like "
        "'in 2 hours', 'at 5pm', 'tonight', 'tomorrow morning', or 'wednesday'."
    )


@dataclass
class Reminder:
    id: str
    message: str
    fire_at: datetime
    created_at: datetime = field(default_factory=datetime.now)
    recurrence: "str | None" = None   # None = one-shot; "daily" or "weekly:0-6" otherwise — see parse_recurrence/next_occurrence
    priority: str = "normal"          # "low" | "normal" | "high" — informational/sortable, not enforced anywhere yet
    status: str = "pending"           # "pending" while in the active list; reminders leave the list entirely on completion/cancellation rather than being marked and kept (see module docstring for why a persisted completed-history isn't built here)
    completed_at: "datetime | None" = None  # set just before a one-shot reminder is removed / a recurring one is rescheduled, for the fired-event payload — not persisted to disk

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "message": self.message,
            "fire_at": self.fire_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "recurrence": self.recurrence,
            "priority": self.priority,
            "status": self.status,
        }

    @staticmethod
    def from_json(d: dict) -> "Reminder":
        return Reminder(
            id=d["id"],
            message=d["message"],
            fire_at=datetime.fromisoformat(d["fire_at"]),
            # .get() with defaults: a reminders.json written before these
            # fields existed is still loadable, same backward-compat
            # contract as ember_memory.py's ALTER TABLE migration —
            # missing structured fields degrade to sensible defaults
            # rather than a KeyError on startup.
            created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else datetime.now(),
            recurrence=d.get("recurrence"),
            priority=d.get("priority", "normal"),
            status=d.get("status", "pending"),
        )


class ReminderStore:
    """Thread-safe, JSON-backed reminder storage with a background poll
    loop. notify_fn is called with the reminder's message text when it
    fires — CLI-appropriate default is a plain print; swap for a real
    delivery channel once one exists, same contract as ember_proactive.py."""

    def __init__(self, path: str, notify_fn: Callable[[str], None] = print, poll_seconds: int = DEFAULT_POLL_SECONDS):
        self.path = path
        self.notify_fn = notify_fn
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._reminders: "list[Reminder]" = self._load()
        self._stop_event = threading.Event()

    def _load(self) -> "list[Reminder]":
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return [Reminder.from_json(r) for r in raw]
        except (json.JSONDecodeError, OSError, KeyError) as e:
            print(f"[ember_reminders] Couldn't load {self.path} ({e}) — starting with an empty reminder list rather than crashing.")
            return []

    def _save_locked(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump([r.to_json() for r in self._reminders], f, indent=2)
        except OSError as e:
            print(f"[ember_reminders] Warning: couldn't persist reminders ({e}).")

    def add(self, message: str, fire_at: datetime, recurrence: "str | None" = None, priority: str = "normal") -> Reminder:
        reminder = Reminder(id=uuid.uuid4().hex[:8], message=message, fire_at=fire_at, recurrence=recurrence, priority=priority)
        with self._lock:
            self._reminders.append(reminder)
            self._reminders.sort(key=lambda r: r.fire_at)
            self._save_locked()
        return reminder

    def list_active(self) -> "list[Reminder]":
        with self._lock:
            return list(self._reminders)

    def cancel(self, reminder_id: str) -> bool:
        """Cancels by stable ID, not list position — fixes the exact bug
        found in jarvisforember's index-based cancel_telegram_reminder."""
        with self._lock:
            before = len(self._reminders)
            self._reminders = [r for r in self._reminders if r.id != reminder_id]
            changed = len(self._reminders) < before
            if changed:
                self._save_locked()
            return changed

    def _poll_loop(self):
        while not self._stop_event.is_set():
            now = datetime.now()
            due = []
            with self._lock:
                still_pending = []
                for r in self._reminders:
                    if r.fire_at <= now:
                        r.completed_at = now
                        due.append(r)
                        if r.recurrence:
                            # Recurring reminder: reschedule rather than
                            # remove — this is the concrete behavior the
                            # next-phase brief asked for ("remind me every
                            # Sunday..."), which the original one-shot-only
                            # design (delete on fire) had no way to express.
                            rescheduled = Reminder(
                                id=r.id, message=r.message, fire_at=next_occurrence(r.fire_at, r.recurrence),
                                created_at=r.created_at, recurrence=r.recurrence, priority=r.priority,
                            )
                            still_pending.append(rescheduled)
                    else:
                        still_pending.append(r)
                if due:
                    self._reminders = still_pending
                    self._reminders.sort(key=lambda r: r.fire_at)
                    self._save_locked()
            for r in due:
                # Publish first (subscribers — e.g. ember_notifications —
                # decide how/whether to surface it), then fall back to the
                # plain notify_fn for anything not on the bus yet. Both
                # firing is intentional during the transition to the bus
                # architecture, not a duplicate-delivery bug: notify_fn is
                # the pre-bus delivery path (still the only one wired up
                # by default — see ember_core.py), and the bus event is
                # what lets a NotificationManager or future subscriber
                # additionally react (log it, escalate a high-priority
                # one, etc.) without ember_reminders.py knowing anything
                # about them.
                try:
                    from ember_bus import get_bus
                    get_bus().publish("reminder.due", {
                        "id": r.id, "message": r.message, "priority": r.priority,
                        "recurrence": r.recurrence, "fire_at": r.fire_at.isoformat(),
                    })
                except Exception as e:
                    print(f"[ember_reminders] bus publish failed for reminder {r.id}: {e}")
                try:
                    self.notify_fn(r.message)
                except Exception as e:
                    print(f"[ember_reminders] notify_fn failed for reminder {r.id}: {e}")
            self._stop_event.wait(timeout=self.poll_seconds)

    def start(self):
        thread = threading.Thread(target=self._poll_loop, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop_event.set()