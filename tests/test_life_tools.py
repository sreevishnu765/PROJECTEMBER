"""The commands around life events and the profile, through the real tool classifier."""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

import ember_core as c
import ember_life as L
import ember_reminders as er
import ember_profile as P


@pytest.fixture()
def core(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(c, "_life_store", L.LifeStore(os.path.join(d, "l.json")))
    monkeypatch.setattr(c, "_profile", P.Profile(os.path.join(d, "p.md")))
    monkeypatch.setattr(c, "_google_calendar_configured", lambda: False)
    monkeypatch.setattr(c, "_reminder_store", er.ReminderStore(path=os.path.join(d, "r.json"), notify_fn=lambda *a, **k: None))
    approve = type("G", (), {"request_confirmation": lambda self, *a, **k: True})()
    monkeypatch.setattr(c, "_confirm_gate", approve)
    return c


def run(core, msg):
    a = core._classify_action(msg)
    return (a[0].name, core._dispatch_action(a, None)) if a else (None, None)


def test_the_whole_story(core):
    name, reply = run(core, "I have a trip in 5 days")
    assert name == "note_life_event" and reply.startswith("Noted, sir") and "in 5 days" in reply
    assert run(core, "when is my trip?")[0] == "when_is_life_event"
    assert "in 5 days" in run(core, "how many days until my trip")[1]
    run(core, "my physics practical is on 3rd november")
    name, listing = run(core, "what's coming up")
    assert name == "list_life_events" and "Trip" in listing and "Physics practical" in listing
    assert "Physics practical" in run(core, "upcoming practicals")[1] and "Trip" not in run(core, "upcoming practicals")[1]
    name, moved = run(core, "move my trip to the 30th")
    assert name == "move_life_event" and "Oct 30" in moved
    name, gone = run(core, "cancel my trip")
    assert name == "remove_life_event" and gone.startswith("Removed")
    assert run(core, "when is my trip?")[0] is None                         # no longer on record -> falls through


def test_moving_a_multi_day_plan_keeps_its_length(core):
    run(core, "trip from the 25th to the 29th")
    _, moved = run(core, "push the trip to the 1st of november")
    assert "Nov 1 to " in moved and "Nov 5" in moved


def test_ambiguous_removal_is_not_guessed(core):
    run(core, "physics practical on 3rd november")
    run(core, "chemistry practical on 5th november")
    name, reply = run(core, "cancel the practical")
    assert name == "remove_life_event_ambiguous" and "won't guess" in reply and len(core._life_store.all()) == 2


def test_removal_is_confirmed_first(core, monkeypatch):
    run(core, "I have a trip in 5 days")
    deny = type("G", (), {"request_confirmation": lambda self, *a, **k: False})()
    monkeypatch.setattr(c, "_confirm_gate", deny)
    assert "not confirmed" in run(core, "cancel my trip")[1] and len(core._life_store.all()) == 1


def test_past_date_is_refused(core):
    name, reply = run(core, "add trip on 5 march 2019")
    assert name == "note_life_event" and "already passed" in reply and not core._life_store.all()


def test_older_tools_keep_their_claims(core):
    assert run(core, "remind me to pack on the 31st of January 2099")[0] == "create_reminder"
    assert run(core, "set a timer for 10 minutes")[0] == "set_timer"
    assert run(core, "what are my reminders?")[0] == "list_reminders"
    assert run(core, "when is my wake up reminder")[0] == "list_reminders"
    assert run(core, "move the budget file to Documents")[0] == "move_file"
    assert run(core, "add trip to my calendar on friday")[0] != "note_life_event"


def test_schedule_with_a_date_and_no_time_goes_to_local_plans(core, monkeypatch):
    name, reply = run(core, "Schedule a scooty part change for the 31st of January 20, 2027.")
    assert name == "note_life_event" and "Scooty part change" in reply
    # timed requests only go to Google when it is connected
    monkeypatch.setattr(c, "_google_calendar_configured", lambda: True)
    assert run(core, "schedule the review at 4pm tomorrow")[0] == "add_calendar_event"
    assert run(core, "schedule scooty service for the 31st of january")[0] == "note_life_event"


def test_calendar_check_includes_local_plans(core):
    core._life_store.add_or_update("Dentist", datetime.now().date(), None, None, datetime.now().date())
    name, reply = run(core, "what's on my schedule today")
    assert name == "check_calendar" and "Dentist" in reply


def test_profile_commands(core):
    assert run(core, "add to my profile: I'm in 5th semester, mechanical")[0] == "profile_add"
    assert "5th semester" in run(core, "show my profile")[1]
    assert "5th semester" in core.build_system_prompt()
    assert run(core, "remove semester from my profile")[1].startswith("Removed from your profile")
    assert "5th semester" not in core.build_system_prompt()


def test_plans_and_persona_reach_the_system_prompt(core):
    run(core, "I have a trip in 5 days")
    prompt = core.build_system_prompt()
    assert "Trip: " in prompt and "in 5 days" in prompt and "How Ember sounds" in prompt
    assert prompt.index("You are Ember") < prompt.index("How Ember sounds") < prompt.index("Trip: ")
