import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ember_voice_text import (  # noqa: E402
    CODE_NOTE, SentenceChunker, clean_for_speech, detect_wake,
    ember_like, is_stop_command, is_stt_garbage, looks_like_echo, parse_voice_mode_command, strip_wake_residue,
)


def stream(text, size):
    ch = SentenceChunker()
    out = []
    for i in range(0, len(text), size):
        out += ch.feed(text[i:i + size])
    out += ch.flush()
    return out


class CleanTests(unittest.TestCase):
    def test_markdown_stripped(self):
        self.assertEqual(clean_for_speech("**Cloud (Gemini):** available"), "Cloud (Gemini): available")
        self.assertEqual(clean_for_speech("See [the docs](https://x.com/a) now"), "See the docs now")
        self.assertEqual(clean_for_speech("Visit https://example.com/a?b=1 today"), "Visit a link today")
        self.assertEqual(clean_for_speech("Run `git status` first"), "Run git status first")
        self.assertEqual(clean_for_speech("## Heading"), "Heading.")
        self.assertEqual(clean_for_speech("- item one\n- item two"), "item one. item two.")
        self.assertEqual(clean_for_speech("1. first\n2. second"), "first. second.")

    def test_snake_case_survives(self):
        self.assertEqual(clean_for_speech("set my_var_name to five"), "set my_var_name to five")

    def test_emoji_and_dashes(self):
        self.assertEqual(clean_for_speech("Done \U0001F525 \u2014 all set"), "Done, all set")

    def test_table(self):
        t = "| a | b |\n|---|---|\n| 1 | 2 |"
        self.assertEqual(clean_for_speech(t), "a, b. 1, 2.")

    def test_code_block_removed(self):
        self.assertEqual(clean_for_speech("Before\n```py\nx=1\n```\nAfter"), "Before After")


class ChunkerTests(unittest.TestCase):
    def test_basic_split_and_merge(self):
        text = "Good evening, sir. Your calendar is clear until four. Anything else?"
        out = stream(text, 1000)
        self.assertEqual(out[0], "Good evening, sir.")
        self.assertTrue(all(o.strip() for o in out))
        self.assertEqual(" ".join(out), text)

    def test_streaming_is_size_independent(self):
        text = (
            "Good evening, sir. It is 6:30 p.m. and the price is 3.5 dollars. "
            "Dr. Smith called about the meeting.\n\n- **Item one**\n- Item two\n\nThat is everything, sir."
        )
        baseline = stream(text, 10_000)
        for size in (1, 2, 3, 5, 7, 13, 50):
            self.assertEqual(stream(text, size), baseline, f"chunk size {size}")

    def test_abbreviations_and_decimals_not_split(self):
        out = stream("It starts at 6 p.m. tomorrow, and Dr. Smith will attend. The fee is 3.5 dollars.", 1000)
        joined = " ".join(out)
        self.assertIn("6 p.m. tomorrow", joined)
        self.assertIn("Dr. Smith", joined)
        self.assertIn("3.5 dollars", joined)
        for o in out:
            self.assertFalse(o.endswith("p.m.") and len(o) < 20 and o != out[-1])

    def test_first_chunk_goes_early(self):
        ch = SentenceChunker()
        out = ch.feed("Certainly, sir. Let me check the")
        self.assertEqual(out, ["Certainly, sir."])

    def test_short_sentences_merge_after_first(self):
        out = stream("Certainly, sir. Yes. No. Maybe. Right then, we proceed to the next item now.", 1000)
        self.assertEqual(out[0], "Certainly, sir.")
        self.assertEqual(out[1], "Yes. No. Maybe. Right then, we proceed to the next item now.")

    def test_code_fence_dropped_and_noted_once(self):
        text = "Here you go, sir.\n```python\nprint('hi')\n```\nAnd another:\n```js\nx()\n```\nDone."
        for size in (1, 2, 4, 1000):
            out = stream(text, size)
            joined = " ".join(out)
            self.assertNotIn("print", joined)
            self.assertNotIn("`", joined)
            self.assertEqual(out.count(CODE_NOTE), 1, f"size {size}: {out}")
            self.assertIn("Done.", joined)
            self.assertIn("Here you go, sir.", joined)

    def test_split_fence_marker_across_deltas(self):
        ch = SentenceChunker()
        out = []
        for d in ["Look:\n``", "`py\ncode here\n``", "`\nOK then, that is all for now."]:
            out += ch.feed(d)
        out += ch.flush()
        joined = " ".join(out)
        self.assertNotIn("code here", joined)
        self.assertNotIn("`", joined)
        self.assertIn("that is all for now", joined)

    def test_first_chunk_cut_at_first_clause_for_latency(self):
        text = ("It's an Austrian energy drink brand founded in 1987, born from a modified Thai formula "
                "called Krating Daeng, and it sold nearly 14 billion cans globally last year, sir.")
        for size in (1, 3, 7, 40, 10_000):
            out = stream(text, size)
            self.assertEqual(out[0], "It's an Austrian energy drink brand founded in 1987,", f"size {size}: {out}")
            self.assertEqual(" ".join(out), text, f"size {size}")

    def test_first_chunk_available_before_sentence_ends(self):
        ch = SentenceChunker()
        out = ch.feed("It's an Austrian energy drink brand founded in 1987, born from a mod")
        self.assertEqual(out, ["It's an Austrian energy drink brand founded in 1987,"])

    def test_first_chunk_short_even_without_any_comma(self):
        text = "The Porsche 963 (Type 9R0) is an LMDh sports prototype racing car designed by Porsche and built by Multimatic."
        for size in (1, 5, 10_000):
            out = stream(text, size)
            self.assertLessEqual(len(out[0]), 60, out)
            self.assertGreaterEqual(len(out[0]), 45, out)
            self.assertEqual(" ".join(out), text, f"size {size}")

    def test_heading_then_long_sentence_starts_quickly(self):
        text = "### Overview\nThe Porsche 963 (Type 9R0) is an LMDh sports prototype racing car designed by Porsche and built by Multimatic."
        for size in (1, 10_000):
            out = stream(text, size)
            self.assertLessEqual(len(out[0]), 65, out)
            self.assertNotIn("#", " ".join(out))

    def test_no_pointless_cut_when_tail_is_tiny(self):
        out = stream("Nothing on the calendar for tomorrow at all, sir.", 1000)
        self.assertEqual(out, ["Nothing on the calendar for tomorrow at all, sir."])

    def test_ramp_caps_second_and_third_chunk(self):
        clause = "this clause keeps going for a while, "
        text = "Opening thought that is long enough to cut here, " + clause * 14 + "and finally done."
        out = stream(text, 10_000)
        self.assertLessEqual(len(out[1]), 95)
        self.assertLessEqual(len(out[2]), 145)
        self.assertEqual(" ".join(out), text.strip())

    def test_long_unpunctuated_run_is_force_split(self):
        text = "word " * 120
        out = stream(text, 1000)
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(o) <= 230 for o in out))

    def test_no_speakable_content_yields_nothing(self):
        self.assertEqual(stream("---\n\n***\n", 5), [])

    def test_unterminated_tail_flushes(self):
        self.assertEqual(stream("Hello there, sir", 3), ["Hello there, sir"])


class WakeTests(unittest.TestCase):
    def check(self, text, matched, command="", position=None):
        r = detect_wake(text)
        self.assertEqual(r.matched, matched, text)
        if matched:
            self.assertEqual(r.command, command, text)
            self.assertEqual(r.position, position, text)

    def test_leading(self):
        self.check("Ember, what's on my calendar tomorrow?", True, "what's on my calendar tomorrow?", "leading")
        self.check("Hey Ember what time is it", True, "what time is it", "leading")
        self.check("okay ember, remind me to call mom at 5pm", True, "remind me to call mom at 5pm", "leading")
        self.check("Hey, Amber, status", True, "status", "leading")
        self.check("Ember.", True, "", "leading")
        self.check("hey ember", True, "", "leading")

    def test_wake_up(self):
        self.check("Wake up.", True, "", "leading")
        self.check("Wake up, Ember.", True, "", "leading")
        self.check("wake up ember what's the weather", True, "what's the weather", "leading")

    def test_trailing(self):
        self.check("What time is it, Ember?", True, "What time is it", "trailing")
        self.check("play some jazz hey ember", True, "play some jazz", "trailing")

    def test_whisper_mishearings_of_ember(self):
        self.check("Hey M. What can you tell me about the Porsche 963?", True, "What can you tell me about the Porsche 963?", "leading")
        self.check("okay em, what's the time", True, "what's the time", "leading")
        self.check("Remember what is the time?", True, "what is the time?", "leading")
        self.check("Remember how many laps there are?", True, "how many laps there are?", "leading")

    def test_mishearing_tolerance_stays_narrow(self):
        for t in ("remember to buy milk", "remember what I said", "Remember what is the time",
                  "hey what time is it", "I'm up early today",
                  "em dash usage is odd", "m is a letter"):
            self.check(t, False)

    def test_negatives(self):
        for t in (
            "what's the weather like", "I need to embed this file", "remember to buy milk",
            "the amber light was on", "december is cold", "wake me up at seven",
            "hey there how are you", "member when we went", "",
        ):
            self.check(t, False)

    def test_bare_alias_is_single_token_only_when_alone(self):
        # a lone "Ember" is a wake with an empty command, not a trailing match
        self.check("Ember", True, "", "leading")

    def test_alias_override(self):
        # narrowing the alias list only narrows anything once the sound-pattern matching is off too
        with unittest.mock.patch.dict(os.environ, {"EMBER_WAKE_ALIASES": "ember", "EMBER_WAKE_LOOSE": "0"}):
            self.check("amber alert issued", False)
            self.check("ember alert issued", True, "alert issued", "leading")
        with unittest.mock.patch.dict(os.environ, {"EMBER_WAKE_ALIASES": "ember"}):       # loose still on
            self.check("amber alert issued", True, "alert issued", "leading")


class LooseWakeTests(unittest.TestCase):
    """Recall-first matching, using the transcripts from the user's own recordings and logs."""

    def check(self, text, matched, command=""):
        r = detect_wake(text)
        self.assertEqual(r.matched, matched, text)
        if matched:
            self.assertEqual(r.command, command, text)

    def test_what_whisper_actually_wrote_for_ember(self):
        self.check("Remember, remind me to call mom", True, "remind me to call mom")      # "Ember, remind me to call mom"
        self.check("Remember, remind me to call mom.", True, "remind me to call mom.")
        self.check("Hey Mbo", True, "")
        self.check("Humba", True, "")
        self.check("Hey Mbo, what's the weather", True, "what's the weather")
        self.check("Hey, I'm Bo, what time is it", True, "what time is it")
        self.check("M boy, which is the next race", True, "which is the next race")
        self.check("m-bye boys more on", True, "boys more on")                            # was "Ember, voice mode on"
        self.check("Enbow, what time is it", True, "what time is it")
        self.check("Humber which is the next Formula One race weekend", True, "which is the next Formula One race weekend")
        self.check("what time is it Mbo", True, "what time is it")                        # trailing form

    def test_ordinary_sentences_are_left_alone(self):
        for t in ("remember to buy milk", "Remember what I said", "I'm back", "I'm bored", "in bed by nine",
                  "on board the ship", "number seven is next", "the member list", "umbrella is wet", "embed this file",
                  "embark on the trip", "ambient light is low", "timber prices", "December is cold", "hey there, how are you",
                  "I'm Bob", "hey I'm back home"):
            self.check(t, False)

    def test_can_be_switched_off(self):
        with unittest.mock.patch.dict(os.environ, {"EMBER_WAKE_LOOSE": "0"}):
            self.check("Humba", False)
            self.check("Remember, remind me to call mom", False)
            self.check("Hey Mbo", False)
            self.check("Ember, what time is it", True, "what time is it")             # the named aliases still work

    def test_ember_like_pattern(self):
        for w in ("ember", "amber", "umber", "humber", "humba", "mbo", "enbow", "embar", "imbo", "mboy"):
            self.assertTrue(ember_like(w), w)
        for w in ("number", "member", "remember", "timber", "december", "embed", "embark", "umbrella", "ambulance",
                  "ambient", "hello", "hey", "a"):
            self.assertFalse(ember_like(w), w)

    def test_residue_stripping_knows_the_loose_forms(self):
        self.assertEqual(strip_wake_residue("Humba, what time is it"), "what time is it")
        self.assertEqual(strip_wake_residue("Hey Mbo what's up"), "what's up")


class VoiceModeTests(unittest.TestCase):
    def test_on(self):
        for t in ("voice mode", "Voice mode on.", "enter voice mode", "start voice mode, please", "let's talk", "Let's chat!"):
            self.assertEqual(parse_voice_mode_command(t), "on", t)

    def test_off(self):
        for t in ("voice mode off", "exit voice mode", "stop listening", "That's all.", "that's all for now, thanks",
                  "we're done", "go to sleep", "stop voice mode"):
            self.assertEqual(parse_voice_mode_command(t), "off", t)

    def test_none(self):
        for t in ("the voice mode setting is confusing", "what is voice mode", "stop the music",
                  "that's all I could find on the topic", "let's talk about the budget", "status"):
            self.assertIsNone(parse_voice_mode_command(t), t)


class StopTests(unittest.TestCase):
    def test_stop(self):
        for t in ("stop", "Stop.", "quiet", "be quiet please", "never mind", "That's enough", "shut up"):
            self.assertTrue(is_stop_command(t), t)
        for t in ("stop the AQI forecasting system", "stop the music", "what is a stop loss", "quiet down the fan"):
            self.assertFalse(is_stop_command(t), t)


class GuardTests(unittest.TestCase):
    def test_garbage(self):
        for t in ("Thank you.", "you", ".", "", "a", "Thanks for watching!"):
            self.assertTrue(is_stt_garbage(t), t)
        for t in ("what time is it", "yes", "no thanks sir"):
            self.assertFalse(is_stt_garbage(t), t)

    def test_echo(self):
        spoken = ["Good evening, sir. Your calendar is clear until four."]
        self.assertTrue(looks_like_echo("your calendar is clear until four", spoken))
        self.assertTrue(looks_like_echo("Good evening sir your calendar is clear", spoken))
        self.assertFalse(looks_like_echo("what's the weather tomorrow", spoken))
        self.assertFalse(looks_like_echo("clear the calendar for friday", spoken))
        self.assertFalse(looks_like_echo("anything", []))
        self.assertTrue(looks_like_echo("Sir?", ["Sir?"]))
        self.assertFalse(looks_like_echo("yes", ["yes sir certainly"]))


if __name__ == "__main__":
    unittest.main()
