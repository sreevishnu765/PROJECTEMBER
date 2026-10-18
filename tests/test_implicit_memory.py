"""Implicit memory saving: only lasting facts about the user, never moods / noise / passing remarks."""
import os, sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ember_core import extract_memory_candidate as extract


@pytest.mark.parametrize("msg", [
    # the exact junk that ended up in the Memory panel
    "I'm in keyboard but I still can't find it.",
    "Already the entire shell I'm coming to you.",
    "I am bored.",
    # other everyday remarks that used to match
    "I'm hungry", "I'm going to the gym", "I am trying to fix the build", "I'm not sure about that",
    "I like that answer", "I like it", "I'm a bit tired", "I'm a little confused", "I'm at the airport",
    "I've got a headache", "I've got a meeting at 5", "thanks, I'm done for today",
    "that was great, I'm so happy with it", "ok so I am looking at the screen",
    "what I like is unclear", "when I'm home I will check", "I hate waiting for builds to finish and I'm tired of it. It never ends.",
    "when's my birthday?", "Am I a good person?",
])
def test_passing_remarks_are_not_stored(msg):
    assert extract(msg) is None


@pytest.mark.parametrize("msg", [
    "My name is Vishnu", "I live in Navi Mumbai", "I work as a student developer", "I'm from India",
    "I am a student", "I'm 21 years old", "I prefer dark mode", "I love Formula 1", "I hate cilantro",
    "My favorite color is gulf blue", "my favourite driver is Kevin Estre", "My birthday is in May",
    "Okay so I live in Navi Mumbai", "fyi I work at a startup", "I own a Nothing Phone", "I'm studying computer science",
])
def test_lasting_facts_are_stored(msg):
    assert extract(msg) == msg.strip()


def test_explicit_remember_still_works():
    assert extract("remember that I am bored of this chair") == "I am bored of this chair"
    assert extract("I'm in the garage, remember") == "I'm in the garage"
