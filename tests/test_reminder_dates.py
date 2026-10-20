"""Reminders on a calendar date: "remind me to ... on the 31st of January 2027"."""
import os, tempfile
from datetime import datetime
import pytest
import ember_reminders as er

NOW = datetime(2026, 10, 20, 12, 0)
Y = datetime.now().year + 1


@pytest.mark.parametrize("text,phrase", [
    ("change scooty parts on the 31st of January 2027", "on the 31st of January 2027"),
    ("pay rent at 5pm on 31/01/2027", "at 5pm on 31/01/2027"),
    ("buy tyres january 31 at 5pm", "january 31 at 5pm"),
    ("call mum on the 3rd", "on the 3rd"),
    ("call mum on the 31st of January 20, 2017.", "on the 31st of January 20, 2017"),
])
def test_date_phrases_are_found(text, phrase):
    assert er.TIME_PHRASE_RE.search(text).group(0) == phrase


@pytest.mark.parametrize("text", ["about the 3rd quarter report", "buy 2 mangoes", "stand by me", "the 2nd floor"])
def test_not_dates(text):
    assert not er.TIME_PHRASE_RE.search(text)


def test_parsed_values():
    assert er.parse_natural_time("on the 31st of january 20, 2017", NOW)[0] == datetime(2027, 1, 31, 9, 0)
    assert er.parse_natural_time("at 5pm on 31/01/2027", NOW)[0] == datetime(2027, 1, 31, 17, 0)
    assert er.parse_natural_time("january 31 at 5pm", NOW)[0] == datetime(2027, 1, 31, 17, 0)


@pytest.fixture()
def core(monkeypatch):
    import ember_core as c
    monkeypatch.setattr(c, "_reminder_store", er.ReminderStore(path=os.path.join(tempfile.mkdtemp(), "r.json"), notify_fn=lambda *a, **k: None))
    return c


def run(core, msg):
    action = core._classify_action(msg)
    assert action and action[0].name == "create_reminder"
    return core._dispatch_action(action, None)


def test_create_on_a_date(core):
    reply = run(core, f"remind me to change the scooty parts on the 31st of January {Y} at 5pm")
    assert reply.startswith("Reminder set, sir") and "change the scooty parts" in reply
    assert f"05:00 PM on Jan 31, {Y}".lstrip("0") in reply or f"5:00 PM on Jan 31, {Y}" in reply
    r = core._reminder_store.list_active()[0]
    assert r.fire_at == datetime(Y, 1, 31, 17, 0) and r.message == "change the scooty parts"


def test_garbled_year_does_not_leak_into_the_text(core):
    reply = run(core, "remind me to change the scooty parts on the 31st of January 20, 2017.")
    assert reply.startswith("Reminder set, sir")
    r = core._reminder_store.list_active()[0]
    assert r.message == "change the scooty parts" and r.fire_at.month == 1 and r.fire_at.day == 31
    assert r.fire_at > datetime.now()


def test_past_year_is_refused(core):
    reply = run(core, "remind me to renew the pass on 5 March 2019")
    assert "already passed" in reply and not core._reminder_store.list_active()


def test_list_shows_year_for_far_dates(core):
    run(core, f"remind me to pay rent on 31 January {Y}")
    out = core._dispatch_action(core._classify_action("what are my reminders?"), None)
    assert f"Jan 31, {Y}" in out and "[" not in out
