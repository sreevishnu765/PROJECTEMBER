"""Reminders with a plain clock time ("set a reminder for 4pm to ...") and no ID in the confirmation."""
import os, sys, tempfile
from datetime import datetime
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ember_reminders as er


NOW = datetime(2026, 10, 18, 14, 0)


@pytest.mark.parametrize("phrase,hour,minute", [
    ("4pm", 16, 0), ("for 4pm", 16, 0), ("at 4pm", 16, 0), ("4 pm", 16, 0), ("4:30pm", 16, 30),
    ("16:00", 16, 0), ("4 o'clock", 16, 0), ("by 4:30", 16, 30), ("4 p.m.", 16, 0), ("at 4", 16, 0),
])
def test_clock_phrases_parse(phrase, hour, minute):
    m = er.TIME_PHRASE_RE.search(phrase)
    assert m and m.group(0).lower() == phrase.lower()
    dt, err = er.parse_natural_time(m.group(0), NOW)
    assert dt == NOW.replace(hour=hour, minute=minute), err


def test_past_clock_time_rolls_to_tomorrow():
    dt, _ = er.parse_natural_time("at 9am", NOW)
    assert dt == datetime(2026, 10, 19, 9, 0)


def test_for_n_people_is_not_a_time():
    assert not er.TIME_PHRASE_RE.search("buy milk for 2 people")
    assert not er.TIME_PHRASE_RE.search("stand by me")


@pytest.fixture()
def core():
    import ember_core as c
    c._reminder_store = er.ReminderStore(path=os.path.join(tempfile.mkdtemp(), "r.json"), notify_fn=lambda *a, **k: None)
    return c


@pytest.mark.parametrize("msg", [
    "Set a reminder for 4pm to turn on the laptop.",
    "set a reminder to turn on the laptop at 4pm",
    "remind me at 4pm to turn on the laptop",
    "remind me 4pm to turn on the laptop",
    "add a reminder for 4 pm to turn on the laptop",
])
def test_create_reminder_with_clock_time(core, msg):
    action = core._classify_action(msg)
    assert action and action[0].name == "create_reminder"
    reply = core._dispatch_action(action, None)
    assert reply.startswith("Reminder set, sir")
    assert "turn on the laptop" in reply and "4:00 PM" in reply
    assert "ID" not in reply


def test_timer_and_duplicate_replies_have_no_id(core):
    msg = "set a reminder for 4pm to stretch"
    core._dispatch_action(core._classify_action(msg), None)
    dup = core._dispatch_action(core._classify_action(msg), None)
    assert dup.startswith("You already have that one set") and "ID" not in dup
    timer = core._dispatch_action(core._classify_action("set a timer for 10 minutes"), None)
    assert timer.startswith("Timer set") and "ID" not in timer
