import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ember_voice_text import clean_for_speech, confirmation_prompt, parse_confirmation_reply  # noqa: E402
from test_voice_session import Rig  # noqa: E402
from test_voice_speaker_gate import FakeSpeaker  # noqa: E402


class SpeechPathTests(unittest.TestCase):
    def test_paths_are_not_read_out(self):
        t = clean_for_speech("Moved the file 'drivetrain_comparision.pdf' from C:\\Users\\sindh\\Downloads to D:\\VISHNU, sir. (Say \"undo that\" to reverse it.)")
        self.assertNotIn("\\", t)
        self.assertNotIn(":", t)
        self.assertEqual(t, "Moved the file 'drivetrain comparision pdf' from Downloads to VISHNU, sir.")

    def test_undo_hint_dropped_and_drive_root(self):
        self.assertEqual(clean_for_speech("Added 'D:\\' to the list. (Say \"x\" later.)"), "Added 'the D drive' to the list.")

    def test_ordinary_text_untouched(self):
        for t in ["Done at 4:30 PM. Dr. Smith agrees.", "It's 3/4 and/or 1.2.3.", "Open a link: see a link"]:
            self.assertEqual(clean_for_speech(t), t)

    def test_posix_and_spaces(self):
        self.assertEqual(clean_for_speech("Saved to /home/me/data/exports/report.pdf now."), "Saved to report pdf now.")
        self.assertEqual(clean_for_speech("In C:\\Users\\me\\My Documents\\Notes ok."), "In Notes ok.")


class ParseReplyTests(unittest.TestCase):
    def test_yes_no(self):
        for t in ["Yes.", "yeah go ahead", "Ember, yes", "do it", "okay", "sure"]:
            self.assertEqual(parse_confirmation_reply(t), "yes", t)
        for t in ["No", "no don't", "stop", "cancel that", "never mind", "don't do it"]:
            self.assertEqual(parse_confirmation_reply(t), "no", t)

    def test_unclear_is_not_approval(self):
        for t in ["yes but wait", "yes what file", "maybe", "the weather today is nice and so on yes", "thank you"]:
            self.assertIsNone(parse_confirmation_reply(t), t)

    def test_prompt_has_no_path(self):
        p = confirmation_prompt("allow_path", {"matched_text": "allow access to C:\\Users\\sindh\\Downloads"})
        self.assertEqual(p, "Allow access to Downloads, sir, yes or no?")


class VoiceConfirmTests(unittest.TestCase):
    def rig(self, speaker=None):
        r = Rig()
        if speaker:
            r.v._speaker = speaker
        r.v.handle_control({"action": "spoken_confirm", "on": True})
        self.addCleanup(r.close)
        return r

    def ask(self, r, answers):
        r.v.ask_confirmation("rq1", "Allow access to Downloads, sir, yes or no?", answers.append)
        r.wait(lambda: r.v._tts_inflight == 0)   # she has finished asking...
        r.advance(5.0)                           # ...and the deaf tail has passed

    def test_yes_without_wake_word_approves(self):
        r, got = self.rig(), []
        self.ask(r, got)
        r.say("yes")
        self.assertEqual(got, [True])
        self.assertEqual(r.submitted, [])           # not sent to the LLM as a chat turn

    def test_no_denies_and_stop_denies(self):
        for word in ("no", "stop"):
            r, got = self.rig(), []
            self.ask(r, got)
            r.say(word)
            self.assertEqual(got, [False], word)

    def test_other_voice_refused(self):
        r, got = self.rig(FakeSpeaker(verdict=False)), []
        self.ask(r, got)
        r.say("yes")
        self.assertEqual(got, [])

    def test_off_when_not_enabled_or_blocked_tool(self):
        r = self.rig()
        self.assertFalse(r.v.can_ask_confirmation("run_script"))
        self.assertTrue(r.v.can_ask_confirmation("allow_path"))
        r.v.handle_control({"action": "spoken_confirm", "on": False})
        self.assertFalse(r.v.can_ask_confirmation("allow_path"))

    def test_click_cancels_pending_so_later_yes_is_chat(self):
        r, got = self.rig(), []
        self.ask(r, got)
        r.v.cancel_confirmation("rq1")
        r.say("yes")
        self.assertEqual(got, [])

    def test_expired_request_not_answered(self):
        r, got = self.rig(), []
        self.ask(r, got)
        r.advance(200)
        r.say("yes")
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
