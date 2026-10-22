"""Morning briefing: once per day, after 6am, honest about a calendar that can't be reached."""
import os
from datetime import datetime, timedelta

import pytest

import ember_briefing as b
from ember_reminders import ReminderStore


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("EMBER_BRIEFING", raising=False)
    monkeypatch.delenv("EMBER_BRIEFING_HOUR", raising=False)
    b.release()


EVENTS = [{"id": "1", "summary": "Scooty part change", "start_display": "9:00 AM"},
          {"id": "2", "summary": "Dentist", "start_display": "2:30 PM"}]


def ok_fetch(d):
    return "Schedule...", EVENTS


def test_not_due_before_six_but_due_after(tmp_path):
    st = str(tmp_path / "s.json")
    assert not b.is_due(datetime(2026, 10, 21, 1, 0), st)       # 1am start doesn't count as the day's first
    assert not b.is_due(datetime(2026, 10, 21, 5, 59), st)
    assert b.is_due(datetime(2026, 10, 21, 6, 0), st)


def test_once_per_day_then_again_next_day(tmp_path):
    st = str(tmp_path / "s.json")
    now = datetime(2026, 10, 21, 7, 0)
    brief = b.prepare(now, st, ok_fetch, lambda: [], lambda: [], lambda: True)
    assert brief and brief["calendar_ok"]
    assert b.prepare(now, st, ok_fetch, lambda: [], lambda: [], lambda: True) is None     # claimed: a second client gets nothing
    b.mark_done(now, st)
    assert b.prepare(now + timedelta(hours=2), st, ok_fetch, lambda: [], lambda: [], lambda: True) is None
    assert b.prepare(now + timedelta(days=1), st, ok_fetch, lambda: [], lambda: [], lambda: True) is not None


def test_early_start_does_not_use_up_the_day(tmp_path):
    st = str(tmp_path / "s.json")
    assert b.prepare(datetime(2026, 10, 21, 1, 0), st, ok_fetch, lambda: [], lambda: [], lambda: True) is None
    assert b.prepare(datetime(2026, 10, 21, 7, 0), st, ok_fetch, lambda: [], lambda: [], lambda: True) is not None


def test_release_allows_retry(tmp_path):
    st = str(tmp_path / "s.json")
    now = datetime(2026, 10, 21, 7, 0)
    assert b.prepare(now, st, ok_fetch, lambda: [], lambda: [], lambda: True)
    b.release()                                                  # delivery failed
    assert b.prepare(now, st, ok_fetch, lambda: [], lambda: [], lambda: True)


def test_text_and_speech(tmp_path):
    now = datetime(2026, 10, 21, 7, 0)
    store = ReminderStore(path=str(tmp_path / "r.json"), notify_fn=lambda *a, **k: None)
    store.add("turn on the laptop", now.replace(hour=16, minute=0))
    store.add("tomorrow thing", now + timedelta(days=1))
    due = [r for r in store.list_active() if r.fire_at.date() == now.date()]
    text, spoken = b.build_briefing(EVENTS, due, now)
    assert "9:00 AM: Scooty part change" in text and "4:00 PM: turn on the laptop" in text
    assert "You have 2 events on your calendar today." in spoken and "9 AM, Scooty part change." in spoken
    assert "1 reminder" in spoken and "turn on the laptop at 4 PM" in spoken
    assert "tomorrow thing" not in text


def test_empty_day_and_all_day_event():
    now = datetime(2026, 10, 21, 7, 0)
    text, spoken = b.build_briefing([], [], now)
    assert "Nothing on your plans" in text and text == spoken
    t2, s2 = b.build_briefing([{"id": "3", "summary": "Holiday", "start_display": "2026-10-21"}], [], now)
    assert "All day: Holiday" in t2 and "All day, Holiday." in s2


def test_google_failure_is_named_in_the_briefing_and_does_not_block_it(tmp_path):
    st = str(tmp_path / "s.json")
    now = datetime(2026, 10, 21, 7, 0)
    bad = lambda d: ("Couldn't reach Google Calendar, sir: x", [])
    brief = b.prepare(now, st, bad, lambda: [], lambda: [], lambda: True)
    assert brief["google_failed"] and "couldn't reach Google Calendar" in brief["text"]
    assert brief["calendar_ok"]                                   # still counts as delivered: local plans got through


def test_google_not_connected_is_silent(tmp_path):
    st = str(tmp_path / "s.json")
    called = []
    brief = b.prepare(datetime(2026, 10, 21, 7, 0), st, lambda d: called.append(d) or ("x", []), lambda: [], lambda: [], lambda: False)
    assert not called and not brief["google_failed"] and "Google" not in brief["text"]


def test_local_plans_today_and_coming_up(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "SOON_DAYS", 7)
    st = str(tmp_path / "s.json")
    now = datetime(2026, 10, 21, 7, 0)
    plans = [{"id": "a", "title": "Physics practical", "start": "2026-10-21", "end": None, "time": None},
             {"id": "b", "title": "Trip", "start": "2026-10-25", "end": "2026-10-29", "time": None},
             {"id": "c", "title": "Sem exams", "start": "2026-12-12", "end": None, "time": None}]
    brief = b.prepare(now, st, ok_fetch, lambda: [], lambda: plans, lambda: False)
    assert "Physics practical" in brief["text"] and "Trip" in brief["text"] and "Sem exams" not in brief["text"]
    assert "On your plans today: Physics practical, today." in brief["spoken"]
    assert "Coming up: Trip in 4 days." in brief["spoken"]


def test_no_events_is_a_real_empty_day(tmp_path):
    st = str(tmp_path / "s.json")
    brief = b.prepare(datetime(2026, 10, 21, 7, 0), st, lambda d: ("No events on 2026-10-21, sir.", []), lambda: [], lambda: [], lambda: True)
    assert not brief["google_failed"] and "Nothing on your plans" in brief["text"]


def test_switch_off(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBER_BRIEFING", "0")
    assert b.prepare(datetime(2026, 10, 21, 7, 0), str(tmp_path / "s.json"), ok_fetch, lambda: [], lambda: [], lambda: True) is None


def test_default_briefing_leaves_a_plan_for_its_own_day(tmp_path):
    st = str(tmp_path / "s.json")
    plans = [{"id": "b", "title": "Booking of tickets", "start": "2026-10-24", "end": None, "time": None}]
    brief = b.prepare(datetime(2026, 10, 21, 7, 0), st, ok_fetch, lambda: [], lambda: plans, lambda: False)
    assert "Booking" not in brief["text"] and "Coming up" not in brief["spoken"]


def test_default_briefing_says_the_plan_on_the_day(tmp_path):
    st = str(tmp_path / "s.json")
    plans = [{"id": "b", "title": "Booking of tickets", "start": "2026-10-24", "end": None, "time": None}]
    brief = b.prepare(datetime(2026, 10, 24, 7, 0), st, ok_fetch, lambda: [], lambda: plans, lambda: False)
    assert "Booking of tickets, today." in brief["spoken"]
