"""Life events: dates found in ordinary speech, kept in a local file, always in front of Ember."""
import os
import tempfile
from datetime import date, datetime

import pytest

import ember_life as L
import ember_profile as P

NOW = datetime(2026, 10, 20, 12, 0)
TODAY = NOW.date()


def cap(text):
    return L.capture(text, NOW)


@pytest.mark.parametrize("text,title,start,end,time", [
    ("I have a trip in 5 days", "Trip", date(2026, 10, 25), None, None),
    ("trip in 5 days", "Trip", date(2026, 10, 25), None, None),
    ("my physics practical is on 3rd november", "Physics practical", date(2026, 11, 3), None, None),
    ("my sem exams are from the 12th to the 24th of december", "Sem exams", date(2026, 12, 12), date(2026, 12, 24), None),
    ("trip from the 25th to the 29th", "Trip", date(2026, 10, 25), date(2026, 10, 29), None),
    ("practicals from nov 20 to nov 30", "Practicals", date(2026, 11, 20), date(2026, 11, 30), None),
    ("exam from 25 nov to 2 dec", "Exam", date(2026, 11, 25), date(2026, 12, 2), None),
    ("I've got a dentist appointment next friday at 5pm", "Dentist appointment", date(2026, 10, 23), None, "17:00"),
    ("i'm having a seminar on nov 9 at 3pm", "Seminar", date(2026, 11, 9), None, "15:00"),
    ("I'm going to Goa on Nov 3 for 4 days", "Trip to Goa", date(2026, 11, 3), date(2026, 11, 6), None),
    ("add scooty service on the 31st", "Scooty service", date(2026, 10, 31), None, None),
    ("Schedule a scooty part change for the 31st of January 2027", "Scooty part change", date(2027, 1, 31), None, None),
    ("note that my viva is on 12 dec", "Viva", date(2026, 12, 12), None, None),
    ("I have a test in 2 weeks", "Test", date(2026, 11, 3), None, None),
    ("exam tomorrow", "Exam", date(2026, 10, 21), None, None),
])
def test_captures(text, title, start, end, time):
    c = cap(text)
    assert c and not c.error, c
    assert (c.title, c.start, c.end, c.time) == (title, start, end, time)


def test_garbled_year_is_ignored_and_the_next_real_date_is_used():
    c = cap("Schedule a scooty part change for the 31st of January 20, 2017.")
    assert c.title == "Scooty part change" and c.start == date(2027, 1, 31) and not c.error


@pytest.mark.parametrize("text", [
    "I have a meeting at 5", "I have a headache", "I have an idea on how to fix it", "my laptop is on the table",
    "I'm going to the gym tomorrow", "I'm going to college on friday", "I have a trip", "hello there",
    "when is my trip in 5 days?", "what time is my exam on friday?", "book tickets for trip in 5 days",
    "I have a trip in 5 days and I'm worried about money", "add milk to my calendar on friday",
    "remind me about the exam on 5 november", "I am bored", "thanks, that was great",
])
def test_not_captured(text):
    assert cap(text) is None


def test_past_year_is_an_error_with_a_suggestion():
    c = cap("add trip on 5 march 2019")
    assert c and "already passed" in c.error and "5 March 2027" in c.error


def test_store_add_update_find_remove_and_persist(tmp_path):
    path = str(tmp_path / "life.json")
    st = L.LifeStore(path)
    ev, action, _ = st.add_or_update("Trip", date(2026, 10, 25), None, None, TODAY)
    assert action == "added"
    assert st.add_or_update("Trip", date(2026, 10, 25), None, None, TODAY)[1] == "unchanged"
    ev2, action, before = st.add_or_update("Trip", date(2026, 10, 26), None, None, TODAY)   # a correction, not a second trip
    assert action == "updated" and ev2["id"] == ev["id"] and before["start"] == "2026-10-25"
    st.add_or_update("Physics practical", date(2026, 11, 3), None, None, TODAY)
    assert [e["title"] for e in L.LifeStore(path).all()] == ["Trip", "Physics practical"]    # survived a reload
    assert [e["title"] for e in st.find("practical", TODAY)] == ["Physics practical"]
    assert st.remove(ev["id"]) and len(st.all()) == 1


def test_corrupt_file_is_kept_aside_not_crashed_on(tmp_path):
    path = tmp_path / "life.json"
    path.write_text("{not json", encoding="utf-8")
    st = L.LifeStore(str(path))
    assert st.all() == [] and (tmp_path / "life.json.bad").exists()


def test_prompt_block_has_dates_and_countdowns(tmp_path):
    st = L.LifeStore(str(tmp_path / "l.json"))
    assert L.prompt_block(st, TODAY) == ""
    st.add_or_update("Trip", date(2026, 10, 25), date(2026, 10, 29), None, TODAY)
    st.add_or_update("Sem exams", date(2027, 6, 1), None, None, TODAY)        # beyond the 60-day window
    block = L.prompt_block(st, TODAY)
    assert "Trip: Sun, Oct 25 to Thu, Oct 29 (in 5 days)" in block and "Sem exams" not in block
    assert "today is Tuesday, October 20, 2026" in block


def test_profile_add_dedupe_remove_and_block(tmp_path):
    p = P.Profile(str(tmp_path / "profile.md"))
    assert p.block() == ""
    assert p.add("I'm in 5th semester, mechanical")[0]
    assert not p.add("i'm in 5th semester mechanical")[0]                      # duplicate
    assert "5th semester" in p.block()
    assert p.find("semester") and p.remove(p.find("semester")[0])
    assert p.block() == ""


def test_profile_cap(tmp_path):
    p = P.Profile(str(tmp_path / "profile.md"))
    for i in range(400):
        ok, msg = p.add(f"fact number {i} " + "x" * 40)
        if not ok:
            assert "full" in msg
            break
    else:
        pytest.fail("profile never hit its size cap")


def test_persona_file_is_loaded_and_reloaded(tmp_path):
    f = tmp_path / "persona.md"
    assert P.persona_block(str(f)) == ""
    f.write_text("Be brief.", encoding="utf-8")
    assert P.persona_block(str(f)) == "Be brief."
    os.utime(f, (1, 1))
    f.write_text("Be briefer.", encoding="utf-8")
    os.utime(f, (2, 2))
    assert P.persona_block(str(f)) == "Be briefer."


def test_shipped_persona_has_no_personal_facts():
    text = P.persona_block()
    assert text and "Examples" in text
