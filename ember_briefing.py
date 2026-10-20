"""Morning briefing: the first time Ember starts up each day, she tells you (aloud, and in the chat) what is on
your plans today (your local plans file, plus Google Calendar if it's connected), any reminders due today, and
what's coming up in the next week.

"Day" starts at EMBER_BRIEFING_HOUR (default 6 = 6am), not at midnight: starting Ember at 1am does not use up
the day's briefing, and a start at 7am gets it. At most one briefing per calendar day, remembered in
data/briefing_state.json so a restart later the same day stays quiet. A briefing is only recorded as done once it
was actually delivered, and a connected Google Calendar that can't be reached is reported honestly in the briefing
instead of being read out as an empty day.

Off switch: EMBER_BRIEFING=0.
"""
import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from typing import Callable, Optional

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_PROJECT_ROOT, "data", "briefing_state.json")
MAX_SPOKEN_ITEMS = 6

_lock = threading.Lock()
_in_flight = False


def enabled() -> bool:
    return os.environ.get("EMBER_BRIEFING", "1").strip().lower() not in ("0", "off", "false", "no")


def briefing_hour() -> int:
    try:
        return max(0, min(23, int(os.environ.get("EMBER_BRIEFING_HOUR", "6"))))
    except ValueError:
        return 6


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[ember_briefing] couldn't save state: {e}")


def is_due(now: datetime, state_path: str = STATE_PATH) -> bool:
    """True when it is past the briefing hour and today's briefing hasn't been given yet."""
    if not enabled() or now.hour < briefing_hour():
        return False
    return _load(state_path).get("last_day") != now.date().isoformat()


def _speak_time(display: str) -> str:
    """'9:00 AM' -> '9 AM' (reads better aloud); '9:30 AM' stays; an all-day date stays out of this."""
    return re.sub(r":00(?= ?[AP]M)", "", display)


def _is_clock(display: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}\s?[AP]M$", display or ""))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def build_briefing(events: "list[dict]", reminders: list, now: datetime, plans_today: "Optional[list]" = None,
                   plans_soon: "Optional[list]" = None, google_failed: bool = False) -> "tuple[str, str]":
    """(text for the chat, text to speak). `events` are Google Calendar dicts (check_schedule()), `reminders` are
    Reminder objects due today (timers already excluded), `plans_today` / `plans_soon` are local plans-file events
    covering today / starting in the next few days."""
    import ember_life
    from ember_reminders import display_message

    today = now.date()
    plans_today, plans_soon = plans_today or [], plans_soon or []
    items = []                                   # (spoken, display) -- Google Calendar events
    for e in events:
        start, name = e.get("start_display", ""), e.get("summary", "(untitled)")
        if _is_clock(start):
            items.append((f"{_speak_time(start)}, {name}.", f"- {start}: {name}"))
        else:
            items.append((f"All day, {name}.", f"- All day: {name}"))
    rem_items = []
    for r in sorted(reminders, key=lambda r: r.fire_at):
        t = r.fire_at.strftime("%I:%M %p").lstrip("0")
        msg = display_message(r.message)
        rem_items.append((f"{msg} at {_speak_time(t)}.", f"- {t}: {msg}"))

    def plan_spoken(e):
        rel = ember_life.relative(e, today)
        return f"{e['title']}, {rel}." if rel in ("today", "starts today") or rel.startswith("on now") else f"{e['title']} {rel}."

    greeting = "Good morning, sir."
    shown, spoken = [greeting], [greeting]
    if plans_today:
        shown.append("On your plans today:\n" + "\n".join(f"- {ember_life.describe(e, today)}" for e in plans_today))
        spoken.append("On your plans today: " + " ".join(plan_spoken(e) for e in plans_today[:MAX_SPOKEN_ITEMS]))
    if items:
        shown.append("On your calendar today:\n" + "\n".join(d for _, d in items))
        spoken.append(f"You have {_plural(len(items), 'event')} on your calendar today.")
        spoken += [s for s, _ in items[:MAX_SPOKEN_ITEMS]]
        if len(items) > MAX_SPOKEN_ITEMS:
            spoken.append(f"And {len(items) - MAX_SPOKEN_ITEMS} more on your calendar.")
    if rem_items:
        shown.append("Reminders today:\n" + "\n".join(d for _, d in rem_items))
        spoken.append(f"And {_plural(len(rem_items), 'reminder')}.")
        spoken += [s for s, _ in rem_items[:MAX_SPOKEN_ITEMS]]
    if plans_soon:
        shown.append("Coming up:\n" + "\n".join(f"- {ember_life.describe(e, today)}" for e in plans_soon))
        spoken.append("Coming up: " + " ".join(plan_spoken(e) for e in plans_soon[:4]))
    if google_failed:
        note = "I couldn't reach Google Calendar just now, so anything only on it is missing."
        shown.append(note)
        spoken.append(note)
    if len(shown) == 1:
        txt = f"{greeting} Nothing on your plans or reminders today, and nothing in the coming week."
        return txt, txt
    return "\n\n".join(shown), " ".join(spoken)


def _default_fetch(date_str: str):
    from ember_tools import calendar as calendar_tool
    return calendar_tool.check_schedule(date_str)


def _default_reminders() -> list:
    from ember_core import _reminder_store
    return _reminder_store.list_active()


def _default_plans() -> list:
    from ember_core import _life_store
    return _life_store.all()


def _default_google_configured() -> bool:
    return os.path.exists(os.path.join(_PROJECT_ROOT, "auth", "token.json"))


SOON_DAYS = 7


def prepare(now: Optional[datetime] = None, state_path: str = STATE_PATH, fetch: Optional[Callable] = None,
            list_reminders: Optional[Callable] = None, list_plans: Optional[Callable] = None,
            google_configured: Optional[Callable] = None) -> "Optional[dict]":
    """Builds today's briefing if one is due, else None. Claims the briefing so two clients connecting at once
    don't both get it; the caller must then call mark_done() after delivering it, or release() if it couldn't.
    Local plans and reminders always make it in; Google Calendar is added only when it's connected, and if it
    is connected but unreachable the briefing says so instead of reading out a falsely empty day.
    Returns {"text", "spoken", "calendar_ok", "google_failed"}."""
    global _in_flight
    now = now or datetime.now()
    with _lock:
        if _in_flight or not is_due(now, state_path):
            return None
        _in_flight = True
    try:
        today = now.date()
        events, google_failed = [], False
        if (google_configured or _default_google_configured)():
            text, events = (fetch or _default_fetch)(now.strftime("%Y-%m-%d"))
            if not events and not text.startswith("No events"):
                google_failed, events = True, []
        try:
            from ember_reminders import is_timer_message
            due = [r for r in (list_reminders or _default_reminders)()
                   if r.fire_at.date() == today and not is_timer_message(r.message)]
        except Exception as e:
            print(f"[ember_briefing] couldn't read reminders: {e}")
            due = []
        plans_today, plans_soon = [], []
        try:
            for e in (list_plans or _default_plans)():
                s_, e_ = date.fromisoformat(e["start"]), date.fromisoformat(e.get("end") or e["start"])
                if s_ <= today <= e_:
                    plans_today.append(e)
                elif today < s_ <= today + timedelta(days=SOON_DAYS):
                    plans_soon.append(e)
        except Exception as e:
            print(f"[ember_briefing] couldn't read plans: {e}")
        shown, spoken = build_briefing(events, due, now, plans_today, plans_soon, google_failed)
        return {"text": shown, "spoken": spoken, "calendar_ok": True, "google_failed": google_failed}
    except Exception:
        release()
        raise


def mark_done(now: Optional[datetime] = None, state_path: str = STATE_PATH, calendar_ok: bool = True) -> None:
    """Record that today's briefing was delivered (only when the calendar actually answered)."""
    global _in_flight
    now = now or datetime.now()
    if calendar_ok:
        _save(state_path, {**_load(state_path), "last_day": now.date().isoformat()})
    with _lock:
        _in_flight = False


def release() -> None:
    global _in_flight
    with _lock:
        _in_flight = False
