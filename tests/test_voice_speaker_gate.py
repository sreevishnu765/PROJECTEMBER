import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_voice_session import Rig  # noqa: E402


class FakeSpeaker:
    """Stands in for SpeakerVerifier: `verdict` is what matches() returns."""
    def __init__(self, enrolled=True, verdict=True):
        self.enrolled = enrolled
        self.verdict = verdict

    def matches(self, audio):
        return self.verdict if self.enrolled else None


class SpeakerGateTests(unittest.TestCase):
    def rig(self, speaker):
        r = Rig()
        r.v._speaker = speaker
        self.addCleanup(r.close)
        return r

    def test_voice_mode_rejects_other_voice_when_enrolled(self):
        r = self.rig(FakeSpeaker(verdict=False))
        r.v.set_voice_mode(True, announce=False)
        r.say("and then he said the funny thing")
        self.assertEqual(r.submitted, [])

    def test_voice_mode_accepts_owner(self):
        r = self.rig(FakeSpeaker(verdict=True))
        r.v.set_voice_mode(True, announce=False)
        r.say("what time is it")
        self.assertEqual(r.submitted, ["what time is it"])

    def test_voice_mode_rejects_too_short_to_tell_when_enrolled(self):
        r = self.rig(FakeSpeaker(verdict=None))
        r.v.set_voice_mode(True, announce=False)
        r.say("what")
        self.assertEqual(r.submitted, [])

    def test_short_clip_fail_open_env_override(self):
        os.environ["EMBER_SPEAKER_STRICT_SHORT"] = "0"
        self.addCleanup(os.environ.pop, "EMBER_SPEAKER_STRICT_SHORT", None)
        r = self.rig(FakeSpeaker(verdict=None))
        r.v.set_voice_mode(True, announce=False)
        r.say("what time is it")
        self.assertEqual(r.submitted, ["what time is it"])

    def test_not_enrolled_behaves_as_before(self):
        r = self.rig(FakeSpeaker(enrolled=False))
        r.v.set_voice_mode(True, announce=False)
        r.say("what time is it")
        self.assertEqual(r.submitted, ["what time is it"])

    def test_stop_exempt_from_gate(self):
        r = self.rig(FakeSpeaker(verdict=False))
        r.v.set_voice_mode(True, announce=False)
        self.assertTrue(r.v._speaker_ok_continuation(None, "stop"))

    def test_follow_up_window_also_gated(self):
        r = self.rig(FakeSpeaker(verdict=False))
        r.v._awake_until = r.now[0] + 30
        r.say("some line from a video")
        self.assertEqual(r.submitted, [])


if __name__ == "__main__":
    unittest.main()
