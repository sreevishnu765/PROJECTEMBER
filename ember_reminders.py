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
from datetime import date, datetime, timedelta
from typing import Callable, Optional

DEFAULT_POLL_SECONDS = 5   # was 20; a 30-second timer must not fire up to 20s late. The check is a local list scan, so this costs nothing.

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

# "in 3 hours and 40 minutes", "in an hour and a half", "in 2 and a half hours", "in half an hour", "in 90 min".
# The old pattern only knew "in <digits> <unit>", so "in 3 hours and 40 minutes" matched just "in 3 hours" and
# the rest ("and 40 minutes to wake up") became the reminder's TEXT.
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
              "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
_DUR_NUM = r"(?:\d+(?:\.\d+)?|half(?:\s+an?)?|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_DUR_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_DUR_ONE = rf"{_DUR_NUM}(?:\s+and\s+a\s+half)?\s*-?\s*{_DUR_UNIT}"
_DURATION_PHRASE = rf"in\s+{_DUR_ONE}(?:\s*(?:,|and|&)?\s*{_DUR_ONE})*(?:\s+and\s+a\s+half)?"
_DUR_PART_RE = re.compile(rf"({_DUR_NUM})(\s+and\s+a\s+half)?\s*-?\s*({_DUR_UNIT})", re.IGNORECASE)


def parse_duration_seconds(text: str) -> "int | None":
    """Total seconds in a duration phrase ("3 hours and 40 minutes" -> 13200), or None if there is none."""
    parts = _DUR_PART_RE.findall((text or "").lower())
    if not parts:
        return None
    total = 0.0
    for qty, and_half, unit in parts:
        qty = qty.strip()
        if qty.startswith("half"):
            n = 0.5
        elif qty in _NUM_WORDS:
            n = float(_NUM_WORDS[qty])
        else:
            try:
                n = float(qty)
            except ValueError:
                continue
        if and_half:
            n += 0.5
        per = 3600 if unit.startswith(("hour", "hr")) else 60 if unit.startswith(("min",)) else 1
        total += n * per
    tail = re.search(r"(?:hours?|hrs?|minutes?|mins?)\s+and\s+a\s+half\s*$", (text or "").lower())
    if tail and not and_half:                       # "an hour and a half": the half belongs to the LAST unit
        total += (1800 if tail.group(0).startswith(("hour", "hr")) else 30)
    return int(total) if total > 0 else None


# A clock time that is unambiguous without a leading word: "4pm", "4 pm", "4:30", "4:30pm", "4 o'clock".
_CLOCK = (r"(?:\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)?"
          r"|\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)"
          r"|\d{1,2}\s+o'?clock)")
# "at 4" (bare hour allowed after "at" only) — "for 2 people" must never read as a time.
_AT_CLOCK = r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?(?:\s+o'?clock)?"
# Leading words are part of the match so they are cut out of the reminder text: "for 4pm" -> "4pm".
_TIME_TAIL = rf"(?:{_AT_CLOCK}|(?:for|by|around|before|at)\s+{_CLOCK}|{_CLOCK})"

# A calendar date in a reminder ("on the 31st of January 2027", "january 31 at 5pm", "at 5pm on 31/01/2027").
# Mirrors the formats _extract_calendar_date() resolves further down. A bare ordinal ("the 3rd") only counts after
# on/for/by, so "remind me about the 3rd quarter report" isn't read as a date. A stray "20, 2017" that speech-to-text
# leaves after a day-first date is swallowed with it (it is ignored when the date is resolved).
_MONTH_NAMES = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december"
                r"|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")
_ORD_SFX = r"(?:st|nd|rd|th)?"
_DATE_CORE = (
    rf"(?:the\s+)?\d{{1,2}}{_ORD_SFX}\s+(?:of\s+)?{_MONTH_NAMES}\b\.?(?:\s*,?\s*\d{{4}}\b|\s+\d{{1,2}}\s*,\s*\d{{4}}\b)?"
    rf"|{_MONTH_NAMES}\b\.?\s+(?:the\s+)?\d{{1,2}}{_ORD_SFX}\b(?:\s*,?\s*\d{{4}}\b)?"
    r"|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}\b"
    r"|\d{4}-\d{2}-\d{2}\b"
)
_DATE_PHRASE = (
    rf"(?:(?:on|for|by)\s+)?(?:{_DATE_CORE})"
    r"|(?:on|for|by)\s+the\s+\d{1,2}(?:st|nd|rd|th)\b"
)
_DATE_WITH_TIME = rf"(?:{_TIME_TAIL}\s+)?(?:{_DATE_PHRASE})(?:\s+{_TIME_TAIL})?"

TIME_PHRASE_RE = re.compile(
    r"\b(" + _DATE_WITH_TIME + r"|" + _DURATION_PHRASE +
    # Day-word phrases (tomorrow/tonight/this evening/...) can optionally
    # be followed by a specific clock time — "tomorrow at 6 PM", "tonight
    # at 9" — and both pieces MUST be captured as one match (re.search
    # only returns the leftmost match, so otherwise the clock time was
    # silently dropped and the reminder defaulted to 9am).
    r"|(?:tomorrow(?:\s+(?:morning|afternoon|evening))?"
    r"|tonight|this\s+evening|this\s+morning|this\s+afternoon"
    r"|evening|morning|afternoon|noon|midnight)"
    rf"(?:\s+{_TIME_TAIL})?"
    # Stand-alone clock times: "at 4pm", "for 4pm", "by 4:30", bare "4pm", "16:00".
    rf"|{_TIME_TAIL}"
    r"|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?![\w:])",
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


# ---- Calendar dates: "31st of January 2027", "January 31, 2027", "31/01/2027", "the 31st" -------------------------
# parse_natural_time() used to know only clock times and day words (tomorrow / weekday names), so "schedule X for
# the 31st of January 2027" failed with "I couldn't figure out a time". Dates are resolved here, then the rest of
# the phrase ("at 5pm") goes through the normal clock-time logic below.
_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
           "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
           "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
_MONTH_RE = r"(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")"
_ORD = r"(?:st|nd|rd|th)?"
# "31st of January" / "31 jan 2027" -- day first. A year only counts when it directly follows the month.
_DMY_RE = re.compile(rf"\b(?:the\s+)?(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_RE})\b\.?(?:\s*,?\s*(\d{{4}})\b)?")
# "January 31st, 2027" / "jan 31" -- month first.
_MDY_RE = re.compile(rf"\b({_MONTH_RE})\b\.?\s+(?:the\s+)?(\d{{1,2}}){_ORD}\b(?:\s*,?\s*(\d{{4}})\b)?")
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")          # 31/01/2027 (day first)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")                         # 2027-01-31
_ORD_DAY_RE = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b")                    # "the 31st"


def _next_occurrence_of(month: int, day: int, today: "date") -> "date | None":
    """Next date on or after `today` with this month/day (skips years where it doesn't exist, e.g. Feb 29)."""
    for y in range(today.year, today.year + 9):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if d >= today:
            return d
    return None


def _pretty_date(d: "date") -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def _find_calendar_date(phrase: str, now: datetime) -> "tuple[date | None, tuple[int, int] | None, str]":
    """Finds a calendar date in an already-lowercased phrase. Returns (date, (start, end) span of the date text, error).
    (None, None, "") means there is no date in it. A year is only trusted right next to the month/day; stray
    digits elsewhere (a speech-to-text garble like "20, 2017" after the month) are ignored, and the resolved date is
    always echoed back with its year in the confirmation so a wrong guess is visible. An explicit year that has
    already passed is refused, with the next real occurrence suggested, rather than creating something in the past."""
    today = now.date()
    day = month = year = None
    span = None
    m = _DMY_RE.search(phrase)
    if m:
        day, month, year = int(m.group(1)), _MONTHS[m.group(2)], (int(m.group(3)) if m.group(3) else None)
        span = m.span()
    if span is None:
        m = _MDY_RE.search(phrase)
        if m:
            month, day, year = _MONTHS[m.group(1)], int(m.group(2)), (int(m.group(3)) if m.group(3) else None)
            span = m.span()
    if span is None:
        m = _NUM_DATE_RE.search(phrase)
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            span = m.span()
    if span is None:
        m = _ISO_DATE_RE.search(phrase)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            span = m.span()
    if span is None:
        m = _ORD_DAY_RE.search(phrase)
        if m:                                              # "the 31st" -> next month that has a 31st
            day = int(m.group(1))
            span = m.span()
            if not 1 <= day <= 31:
                return None, None, f"There's no day {day} in a month, sir."
            y, mo = today.year, today.month
            for _ in range(14):
                try:
                    d = date(y, mo, day)
                except ValueError:
                    d = None
                if d and d >= today:
                    return d, span, ""
                mo += 1
                if mo > 12:
                    mo, y = 1, y + 1
            return None, None, f"I couldn't find the next {day}th, sir."
    if span is None:
        return None, None, ""

    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None, None, "That doesn't look like a real date, sir."
    if year is None:
        d = _next_occurrence_of(month, day, today)
        if d is None:
            return None, None, "That date doesn't exist, sir."
        return d, span, ""
    try:
        d = date(year, month, day)
    except ValueError:
        return None, None, f"{day} {date(2000, month, 1).strftime('%B')} {year} isn't a real date, sir."
    if d < today:
        nxt = _next_occurrence_of(month, day, today)
        hint = f" Did you mean {_pretty_date(nxt)}? Say it again with the right year." if nxt else ""
        return None, None, f"{_pretty_date(d)} has already passed, sir.{hint}"
    return d, span, ""



def _extract_calendar_date(phrase: str, now: datetime) -> "tuple[date | None, str, str]":
    """(date, phrase_with_the_date_removed, error); (None, phrase, "") when there is no date in it."""
    d, span, err = _find_calendar_date(phrase, now)
    if err:
        return None, phrase, err
    if d is None:
        return None, phrase, ""
    return d, phrase[:span[0]] + " " + phrase[span[1]:], ""


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

    dm = re.search(_DURATION_PHRASE, phrase)
    if dm:
        seconds = parse_duration_seconds(dm.group(0))
        if seconds:
            return now + timedelta(seconds=seconds), ""

    given_date, phrase, date_error = _extract_calendar_date(phrase, now)
    if date_error:
        return None, date_error

    is_day_after_tomorrow = (not given_date) and "day after tomorrow" in phrase
    # Real bug: "day after tomorrow" contains the substring "tomorrow",
    # so the plain tomorrow-check below used to fire for it too — off by
    # one day, always. Checked first so the more specific phrase wins.
    is_tomorrow = (not given_date) and (not is_day_after_tomorrow) and "tomorrow" in phrase

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
    weekday_index = (None if given_date else
                     next((i for i, wd in enumerate(_WEEKDAY_NAMES) if re.search(rf"\b{wd}\b", phrase)), None))

    def _ok(dt: datetime) -> "tuple[Optional[datetime], str]":
        """A phrase with an explicit calendar date must land in the future (\"the 19th at 9am\" said at noon on the 19th)."""
        if given_date and dt <= now:
            return None, f"{dt.strftime('%I:%M %p on %d %B %Y').lstrip('0')} has already passed, sir — give me a later time or another day."
        return dt, ""

    if given_date:
        base_day = datetime.combine(given_date, datetime.min.time())
        is_explicit_future_day = True
    elif weekday_index is not None:
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

    phrase = phrase.replace("a.m.", "am").replace("p.m.", "pm")
    phrase = re.sub(r"\b(\d{1,2})\s+o'?clock\b", r"\1:00", phrase)   # "4 o'clock" -> "4:00"
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
        if (meridiem is None and not is_explicit_future_day and 1 <= hour <= 12
                and not m.group(1).startswith("0")):
            # "at 4" with no am/pm: take whichever 4 comes next (said at 2pm -> 4pm today, not 4am tomorrow).
            h12 = hour % 12
            options = []
            for h in (h12, h12 + 12):
                c = now.replace(hour=h, minute=minute, second=0, microsecond=0)
                options.append(c if c > now else c + timedelta(days=1))
            return min(options), ""
        if candidate <= now and not is_explicit_future_day:
            candidate += timedelta(days=1)  # "at 5pm" said at 6pm means tomorrow, not the past
        return _ok(candidate)

    for key in _TIME_OF_DAY_KEYS_ORDERED:
        if key in phrase:
            hour, minute = _TIME_OF_DAY_DEFAULTS[key]
            candidate = base_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now and not is_explicit_future_day:
                candidate += timedelta(days=1)
            return _ok(candidate)

    if is_explicit_future_day:
        return _ok(base_day.replace(hour=9, minute=0, second=0, microsecond=0))

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

# ---- Timers and spoken delivery -------------------------------------------
# A timer is an ordinary one-shot reminder whose message carries this marker, so it needs no new storage,
# survives a restart, and shows up in "list my reminders". reminder_speech() is what the desktop client's
# voice says when it fires.
TIMER_MARKER = "[timer] "


def make_timer_message(duration_text: str, label: str = "") -> str:
    return f"{TIMER_MARKER}{duration_text}" + (f" | {label}" if label else "")


def is_timer_message(message: str) -> bool:
    return (message or "").startswith(TIMER_MARKER)


def reminder_speech(message: str) -> str:
    """What to SAY when a reminder or timer fires. 'submit Alpha' -> 'Reminder to submit Alpha, sir.'"""
    message = (message or "").strip()
    if is_timer_message(message):
        body = message[len(TIMER_MARKER):]
        duration, _, label = (p.strip() for p in body.partition("|"))
        if label:
            return f"Your {duration} timer for {label} is up, sir."
        return f"Your {duration} timer is up, sir."
    if not message or message.lower() in ("this", "it"):
        return "Reminder, sir."
    body = message[0].lower() + message[1:] if message[:2].upper() != message[:2] else message
    if body.lower().startswith("to "):
        body = body[3:]
    return f"Reminder to {body.rstrip('.!?')}, sir."


def display_message(message: str) -> str:
    """Human-readable form of a stored message (hides the timer marker)."""
    if is_timer_message(message):
        duration, _, label = (p.strip() for p in message[len(TIMER_MARKER):].partition("|"))
        return f"{duration} timer" + (f" ({label})" if label else "")
    return message


# ---- Finding reminders by what the person says ------------------------------------------
# "cancel it", "cancel wake up", "cancel the 30 minute reminder", "cancel 0decde46", "cancel the 5pm one".
_ID_RE = re.compile(r"\b[0-9a-f]{8}\b")
_KIND_WORDS = {"reminder", "reminders", "timer", "timers", "alarm", "alarms", "the", "my", "a", "an", "that", "this",
               "one", "ones", "those", "these", "of", "for", "to", "and", "please", "ember", "sir", "active", "all",
               "pending", "upcoming", "current", "me", "it"}
PRONOUNS = {"it", "that", "this", "that one", "this one", "the last one", "last one", "the latest", "the latest one",
            "the last", "that reminder", "this reminder", "that timer", "this timer", "it please"}


def format_when(dt: datetime) -> str:
    """'04:00 PM on Oct 18' -- with the year added when it isn't this year, so a far-off reminder reads right."""
    out = dt.strftime("%I:%M %p on %b %d").lstrip("0")
    return out + (dt.strftime(", %Y") if dt.year != datetime.now().year else "")


def reminder_label(r) -> str:
    return display_message(r.message)


def describe_reminder(r) -> str:
    when = r.fire_at.strftime("%I:%M %p").lstrip("0") + (r.fire_at.strftime(", %b %d") if r.fire_at.date() != datetime.now().date() else "") + (r.fire_at.strftime(", %Y") if r.fire_at.year != datetime.now().year else "")
    return f"{reminder_label(r)} at {when}" + (f" (repeats {r.recurrence})" if r.recurrence else "")


def _words(text: str) -> "list[str]":
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in _KIND_WORDS]


def find_reminders(reminders: list, query: str) -> list:
    """Active reminders that the phrase `query` plausibly refers to. Tries, in order, an exact ID, a clock time
    ("5pm"), a duration ("the 30 minute reminder" = one set for 30 minutes), then the reminder's own words. The
    first signal that finds anything wins, so "cancel 0decde46" never also grabs a reminder whose text happens
    to contain a similar word."""
    q = (query or "").lower().strip()
    if not q:
        return []
    ids = _ID_RE.findall(q)
    if ids:
        return [r for r in reminders if r.id in ids]

    tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", q)
    if tm and not re.search(r"\b(?:minute|min|hour|hr|second|sec)s?\b", q):
        if tm.group(1):
            h, m, mer = int(tm.group(1)), int(tm.group(2) or 0), tm.group(3)
            h = (h % 12) + (12 if mer == "pm" else 0)
        else:
            h, m = int(tm.group(4)), int(tm.group(5))
        hit = [r for r in reminders if r.fire_at.hour == h and r.fire_at.minute == m]
        if hit:
            return hit

    dur = None
    dm = re.search(rf"{_DUR_ONE}(?:\s*(?:,|and|&)?\s*{_DUR_ONE})*", q)
    if dm:
        dur = parse_duration_seconds(dm.group(0))
    if dur:
        hit = [r for r in reminders if abs((r.fire_at - r.created_at).total_seconds() - dur) <= 90
               or (is_timer_message(r.message) and parse_duration_seconds(r.message) == dur)]
        if hit:
            return hit

    words = _words(q)
    if not words and not dm:
        # only the KIND was named ("stop the timer", "cancel reminder"): every item of that kind
        if re.search(r"\btimers?\b", q):
            return [r for r in reminders if is_timer_message(r.message)]
        if re.search(r"\b(?:reminders?|alarms?)\b", q):
            return list(reminders)
    words = [w for w in words if not re.fullmatch(r"\d+|minutes?|mins?|hours?|hrs?|seconds?|secs?", w)] or ([] if dur else words)
    if not words:
        return []
    kind_timer = bool(re.search(r"\btimers?\b", q))
    out = []
    for r in reminders:
        hay = f"{r.message} {reminder_label(r)}".lower()
        if kind_timer and not is_timer_message(r.message):
            continue
        hay_words = set(re.findall(r"[a-z0-9']+", hay))
        # every word the person used must be in the reminder (so "check stream" finds "check the stream")
        if all(w in hay_words or any(h.startswith(w) and len(w) >= 4 for h in hay_words) for w in words):
            out.append(r)
    return out


def all_identical(rs: list) -> bool:
    """True when several reminders are the same thing set more than once (same text, same minute)."""
    if len(rs) < 2:
        return False
    first = rs[0]
    return all(r.message.strip().lower() == first.message.strip().lower()
               and abs((r.fire_at - first.fire_at).total_seconds()) < 90 and r.recurrence == first.recurrence for r in rs[1:])
