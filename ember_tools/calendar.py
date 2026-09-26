"""
ember_tools/calendar.py
=========================
Google Calendar integration, picked up from Nexus VII's
modules/calendar_module.py — a real gap directly named in the
next-phase brief's own capability list ("calendar, weather, Spotify,
email, GitHub, computer control").

Real bug found and fixed: the original's check_schedule() built its day
window like this:

    start_iso = start_time.isoformat() + 'Z'

start_time there is a NAIVE local datetime (parsed with
datetime.strptime, no tzinfo attached) — appending 'Z' asserts to the
Calendar API "this timestamp is UTC," which is only true if the machine
happens to be in the UTC timezone. On the Vivobook (IST, UTC+5:30), this
silently shifted the queried day window by 5.5 hours — "today" would
actually query part of yesterday and miss part of today. Fixed by
attaching the real local tzinfo (via datetime.now().astimezone().tzinfo)
before calling isoformat(), so the ISO string carries a correct explicit
offset instead of a false UTC claim.

Also new here, not in the original: bridges Ember's existing
ember_reminders.parse_natural_time() so callers can say "tomorrow at 3pm"
instead of having to already have an ISO 8601 string in hand — the
original's add_event() required the CALLER (there, the LLM via a tool
schema) to already produce full ISO 8601 strings with explicit offsets,
which works for an LLM-driven tool call but not for Ember's current
regex-triggered registry pattern where the handler gets a raw phrase.
"""

from datetime import datetime, timedelta
import re

from ember_reminders import parse_natural_time
from ember_tools import google_auth

DEFAULT_EVENT_DURATION_MINUTES = 60

# Used by update_event_time() below to decide whether a reschedule phrase
# names a day at all, or is just a bare time ("6pm").
_PHRASE_HAS_DAY_RE = re.compile(
    r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


def _local_isoformat(dt: datetime) -> str:
    """Attaches the machine's real local UTC offset to a naive datetime
    before formatting — see the module docstring for the bug this fixes.
    If `dt` already has tzinfo (e.g. from an ISO string with an explicit
    offset), it's left alone."""
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attaches the system's real local offset
    return dt.isoformat()


def _format_event_start(start_field: dict) -> str:
    """Formats an event's start time for display in local time. Real,
    reported bug: this used to just print event["start"]["dateTime"] raw
    — Google's API returns that in UTC ("...Z"), so a 4pm IST event
    displayed as "10:30:00Z", reading like 10:30 AM to anyone glancing at
    it, while the SAME event was correctly created and confirmed back in
    local time when it was scheduled. Parses the ISO string and converts
    to the machine's local time before formatting, so create and check
    always agree. All-day events (date-only, no dateTime) have no time
    component to convert — shown as-is."""
    raw_datetime = start_field.get("dateTime")
    if not raw_datetime:
        return start_field.get("date", "unknown date")
    # fromisoformat doesn't accept a trailing bare "Z" before Python 3.11;
    # normalize it to an explicit +00:00 offset first so this works
    # regardless of which Python version this ends up running under.
    normalized = raw_datetime.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    local_dt = dt.astimezone()  # convert to the machine's real local timezone
    return local_dt.strftime("%I:%M %p").lstrip("0") or "12:00 AM"


def check_schedule(date_str: str) -> "tuple[str, list[dict]]":
    """date_str: "YYYY-MM-DD". Returns (display_text, events) — never
    raises. `events` is a list of {"id","summary","start_display"} dicts
    (empty on error or no events), added this pass so a caller can
    remember which single event was just shown and act on a pronoun
    follow-up ("cancel that") without the user having to repeat its name
    — see ember_core.py's _check_calendar handler."""
    try:
        service = google_auth.get_calendar_service()
    except Exception as e:
        return f"Couldn't reach Google Calendar, sir: {e}", []

    try:
        start_time = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(days=1)
        start_iso = _local_isoformat(start_time)
        end_iso = _local_isoformat(end_time)

        events_result = service.events().list(
            calendarId="primary", timeMin=start_iso, timeMax=end_iso,
            maxResults=10, singleEvents=True, orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])

        if not events:
            return f"No events on {date_str}, sir.", []

        lines = []
        structured = []
        for event in events:
            start = _format_event_start(event["start"])
            summary = event.get("summary", "(untitled)")
            lines.append(f"  - {start}: {summary}")
            structured.append({"id": event["id"], "summary": summary, "start_display": start})
        return f"Schedule for {date_str}, sir:\n" + "\n".join(lines), structured
    except Exception as e:
        return f"Couldn't fetch the schedule, sir: {e}", []


def check_schedule_natural(phrase: str) -> "tuple[str, list[dict]]":
    """Accepts a natural-language date phrase ("today", "tomorrow") by
    reusing parse_natural_time, rather than requiring the caller to
    already have a YYYY-MM-DD string — the tool-registry handler in
    ember_core.py calls this, not check_schedule() directly."""
    phrase = (phrase or "today").strip() or "today"
    if phrase.lower() in ("today", ""):
        target = datetime.now()
    else:
        target, error = parse_natural_time(phrase)
        if target is None:
            return f"I didn't catch what day you meant, sir: {error}", []
    return check_schedule(target.strftime("%Y-%m-%d"))




def add_event(summary: str, start_phrase: str, duration_minutes: int = DEFAULT_EVENT_DURATION_MINUTES, description: str = "") -> "tuple[str, str | None]":
    """Creates a calendar event from a natural-language start time
    ("tomorrow at 3pm") and a duration in minutes, rather than requiring
    pre-built ISO 8601 strings for both start and end — see the module
    docstring for why this differs from the original.

    Returns (message, event_id). event_id is None on any failure path
    (nothing was created to reference) and a real Google event ID on
    success — added this pass so ember_core.py can remember which event
    it just created and offer a REAL correction via update_event_time()
    below, instead of a follow-up like "4pm, ember." falling through to
    plain chat and getting a fabricated "corrected" reply with no actual
    change made (the exact failure this was built to close)."""
    start_dt, error = parse_natural_time(start_phrase)
    if start_dt is None:
        return f"I didn't catch when to schedule '{summary}', sir: {error}", None
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    try:
        service = google_auth.get_calendar_service()
    except Exception as e:
        return f"Couldn't reach Google Calendar, sir: {e}", None

    try:
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": _local_isoformat(start_dt)},
            "end": {"dateTime": _local_isoformat(end_dt)},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        when = start_dt.strftime("%I:%M %p on %b %d")
        message = f"Added '{summary}' to your calendar at {when}, sir. {created.get('htmlLink', '')}".strip()
        return message, created.get("id")
    except Exception as e:
        return f"Couldn't create the event, sir: {e}", None


def update_event_time(event_id: str, summary: str, start_phrase: str, duration_minutes: int = DEFAULT_EVENT_DURATION_MINUTES) -> str:
    """Actually moves an existing event to a new start time, via a partial
    Google Calendar patch. Without this, a correction like "4pm, ember."
    or a pronoun reschedule like "reschedule that to 6pm" matches no real
    tool, falls through to plain chat, and the model fabricates a
    confident "corrected"/"rescheduled" reply while the real event stays
    put. Existing OAuth scope is full calendar read/write (see
    google_auth.py's SCOPES), so patching an existing event needs no new
    permission grant."""
    try:
        service = google_auth.get_calendar_service()
    except Exception as e:
        return f"Couldn't reach Google Calendar, sir: {e}"

    # Real, reproduced bug: "reschedule that to 6pm" (event was on Sep
    # 17) silently moved it to Sep 16 — TODAY — instead of keeping it on
    # the day it was already scheduled for. parse_natural_time() has no
    # way to know the event wasn't already on today; a bare time phrase
    # with no day mentioned defaults to "today" by its own design (which
    # is correct for a NEW event, but wrong for MOVING an existing one).
    # Fix: only when the phrase names no day at all, fetch the event's
    # current start and use midnight of THAT day as the reference point,
    # so "6pm" resolves relative to the day the event is already on. A
    # phrase that DOES name a day ("reschedule to wednesday 6pm") is left
    # alone — the user is explicitly choosing a different day there.
    reference_now = None
    if not _PHRASE_HAS_DAY_RE.search(start_phrase):
        try:
            existing = service.events().get(calendarId="primary", eventId=event_id).execute()
            existing_start_raw = existing.get("start", {}).get("dateTime")
            if existing_start_raw:
                existing_start = datetime.fromisoformat(existing_start_raw.replace("Z", "+00:00")).astimezone()
                reference_now = existing_start.replace(hour=0, minute=0, second=0, microsecond=0)
        except Exception:
            reference_now = None  # couldn't fetch it — fall back to today, same as before this fix

    start_dt, error = parse_natural_time(start_phrase, now=reference_now)
    if start_dt is None:
        return f"I didn't catch the new time for '{summary}', sir: {error}"
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    try:
        service.events().patch(
            calendarId="primary",
            eventId=event_id,
            body={
                "start": {"dateTime": _local_isoformat(start_dt)},
                "end": {"dateTime": _local_isoformat(end_dt)},
            },
        ).execute()
        when = start_dt.strftime("%I:%M %p on %b %d")
        return f"Updated '{summary}' to {when}, sir."
    except Exception as e:
        return f"Couldn't update the event, sir: {e}"


def find_events_by_keyword(query: str, days_ahead: int = 14) -> "list[dict]":
    """Searches the next `days_ahead` days for events whose summary
    contains `query` (case-insensitive substring) — the lookup step
    behind real cancel/reschedule-by-name below. Returns a list of
    {"id", "summary", "start_display"} dicts, empty if nothing matches.
    Never raises — callers treat an empty list and an exception the same
    way (nothing found), same honest-degradation contract as everywhere
    else in this project."""
    try:
        service = google_auth.get_calendar_service()
    except Exception:
        return []

    try:
        now = datetime.now().astimezone()
        window_end = now + timedelta(days=days_ahead)
        events_result = service.events().list(
            calendarId="primary", timeMin=now.isoformat(), timeMax=window_end.isoformat(),
            maxResults=50, singleEvents=True, orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])
    except Exception:
        return []

    q = query.strip().lower()
    matches = []
    for event in events:
        summary = event.get("summary", "")
        if q and q in summary.lower():
            matches.append({
                "id": event["id"],
                "summary": summary,
                "start_display": _format_event_start(event.get("start", {})),
            })
    return matches


def cancel_event_by_id(event_id: str, summary: str) -> str:
    """Cancels a specific event whose ID is already known — used for
    pronoun follow-ups ("cancel that"/"cancel it") right after a calendar
    check or create, where re-searching by keyword would be redundant
    (and riskier, if two similarly-named events exist). Real gap this
    closes: "please cancel that" right after checking tomorrow's calendar
    matched no registered tool at all (no calendar noun like "meeting" to
    trigger the keyword-based cancel_event above), fell through to plain
    chat, and the model fabricated an entire fake cancel confirmation —
    including, when caught not having worked, a fabricated excuse about
    calendar sync being slow."""
    try:
        service = google_auth.get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return f"Cancelled '{summary}', sir."
    except Exception as e:
        return f"Couldn't cancel '{summary}', sir: {e}"


def cancel_event(query: str) -> str:
    """Finds an upcoming event by keyword and deletes it — the real
    capability that didn't exist at all before this pass. Without this,
    "cancel dentist meeting" matched no registered tool, fell through to
    plain chat, and the model could fabricate an entire fake confirmation
    ("That is a high-risk action, sir. Shall I cancel...") and a fake
    success message with no actual deletion happening. Mirrors
    ember_reminders.py's own fuzzy-match-by-keyword pattern: zero matches
    is an honest "nothing found," more than one is a disambiguation
    question, never a guess."""
    matches = find_events_by_keyword(query)
    if not matches:
        return f"I couldn't find an upcoming event matching '{query}', sir."
    if len(matches) > 1:
        options = "; ".join(f"'{m['summary']}' at {m['start_display']}" for m in matches)
        return f"A few events match '{query}', sir — which one? {options}"

    event = matches[0]
    try:
        service = google_auth.get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event["id"]).execute()
        return f"Cancelled '{event['summary']}', sir."
    except Exception as e:
        return f"Couldn't cancel '{event['summary']}', sir: {e}"


def reschedule_event_by_keyword(query: str, new_start_phrase: str, duration_minutes: int = DEFAULT_EVENT_DURATION_MINUTES) -> str:
    """Finds an upcoming event by keyword and actually moves it, via
    update_event_time() above. Same real-capability gap as cancel_event —
    "reschedule the team meeting to wednesday" previously matched
    nothing, and any apparent success was either a fabricated reply or
    (per user testing) genuinely worked through some other path — this
    makes it a real, dependable tool rather than something to hope
    happens correctly."""
    matches = find_events_by_keyword(query)
    if not matches:
        return f"I couldn't find an upcoming event matching '{query}', sir."
    if len(matches) > 1:
        options = "; ".join(f"'{m['summary']}' at {m['start_display']}" for m in matches)
        return f"A few events match '{query}', sir — which one? {options}"

    event = matches[0]
    return update_event_time(event["id"], event["summary"], new_start_phrase, duration_minutes)
    """Actually moves an existing event to a new start time, via a partial
    Google Calendar patch — the real capability that didn't exist at all
    before this pass. Without this, a correction like "4pm, ember." right
    after creating an event matched no registered tool, fell through to
    plain chat, and the model fabricated a confident "Corrected to 4:00
    PM, sir" reply while the real event silently stayed at its original
    (wrong) time. Existing OAuth scope is full calendar read/write (see
    google_auth.py's SCOPES), so patching an existing event needs no new
    permission grant."""
    start_dt, error = parse_natural_time(start_phrase)
    if start_dt is None:
        return f"I didn't catch the new time for '{summary}', sir: {error}"
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    try:
        service = google_auth.get_calendar_service()
    except Exception as e:
        return f"Couldn't reach Google Calendar, sir: {e}"

    try:
        service.events().patch(
            calendarId="primary",
            eventId=event_id,
            body={
                "start": {"dateTime": _local_isoformat(start_dt)},
                "end": {"dateTime": _local_isoformat(end_dt)},
            },
        ).execute()
        when = start_dt.strftime("%I:%M %p on %b %d")
        return f"Updated '{summary}' to {when}, sir."
    except Exception as e:
        return f"Couldn't update the event, sir: {e}"
