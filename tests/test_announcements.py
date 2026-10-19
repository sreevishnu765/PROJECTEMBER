"""Reminders that are actually heard: other-app ducking (ember_duck) and announcements that incidental
speech cannot cut off (VoiceSession.announce), plus the per-session audio generation start."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ember_duck import AudioDucker, AudioSession  # noqa: E402
from tests.test_voice_session import Rig, silence_bytes, speech_bytes, wait_for  # noqa: E402


class FakeMixer:
    """A stand-in for the Windows volume mixer: name -> [pid, volume, muted]."""

    def __init__(self, apps):
        self.apps = {k: list(v) for k, v in apps.items()}

    def sessions(self):
        out = []
        for key, (pid, vol, muted) in self.apps.items():
            out.append(AudioSession(
                key=key, pid=pid, name=key,
                get_volume=lambda k=key: self.apps[k][1],
                set_volume=lambda x, k=key: self.apps[k].__setitem__(1, x),
                is_muted=lambda k=key: self.apps[k][2],
            ))
        return out

    def vol(self, key):
        return self.apps[key][1]


def make_ducker(mixer, tmp, **kw):
    kw.setdefault("max_seconds", 30.0)
    return AudioDucker(enabled=True, level=0.1, journal_path=os.path.join(tmp, "duck.json"),
                       session_provider=mixer.sessions, protected_pids=lambda: {100}, **kw)


class DuckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mixer = FakeMixer({
            "chrome.exe": (1, 0.8, False),
            "ember.exe": (100, 1.0, False),       # Ember's own audio: protected
            "system sounds": (0, 1.0, False),     # no pid: system sounds
            "muted.exe": (2, 0.5, True),
            "spotify.exe": (3, 0.6, False),
        })

    def test_lowers_others_and_restores_exactly(self):
        d = make_ducker(self.mixer, self.tmp)
        self.assertTrue(d.acquire())
        self.assertAlmostEqual(self.mixer.vol("chrome.exe"), 0.08)
        self.assertAlmostEqual(self.mixer.vol("spotify.exe"), 0.06)
        self.assertEqual(self.mixer.vol("ember.exe"), 1.0)
        self.assertEqual(self.mixer.vol("system sounds"), 1.0)
        self.assertEqual(self.mixer.vol("muted.exe"), 0.5)
        d.release()
        self.assertEqual(self.mixer.vol("chrome.exe"), 0.8)
        self.assertEqual(self.mixer.vol("spotify.exe"), 0.6)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "duck.json")))

    def test_holds_are_counted(self):
        d = make_ducker(self.mixer, self.tmp)
        d.acquire()
        d.acquire()
        d.release()
        self.assertAlmostEqual(self.mixer.vol("chrome.exe"), 0.08)   # one hold still open
        d.release()
        self.assertEqual(self.mixer.vol("chrome.exe"), 0.8)
        d.release()                                                  # extra release is harmless
        self.assertEqual(self.mixer.vol("chrome.exe"), 0.8)

    def test_excluded_apps_untouched(self):
        d = make_ducker(self.mixer, self.tmp, exclude=["spotify"])
        d.acquire()
        self.assertEqual(self.mixer.vol("spotify.exe"), 0.6)
        self.assertAlmostEqual(self.mixer.vol("chrome.exe"), 0.08)
        d.release()

    def test_watchdog_restores_a_forgotten_duck(self):
        d = make_ducker(self.mixer, self.tmp, max_seconds=0.2)
        d.acquire()
        self.assertTrue(wait_for(lambda: self.mixer.vol("chrome.exe") == 0.8, timeout=2.0))

    def test_journal_lets_the_next_run_undo_a_crash(self):
        d = make_ducker(self.mixer, self.tmp)
        d.acquire()
        with open(os.path.join(self.tmp, "duck.json")) as f:
            self.assertEqual(json.load(f)["volumes"]["chrome.exe"], 0.8)
        # The process dies here: no release(). A fresh ducker (the next start) finds the journal.
        d2 = make_ducker(self.mixer, self.tmp)
        d2.recover()
        self.assertEqual(self.mixer.vol("chrome.exe"), 0.8)
        self.assertEqual(self.mixer.vol("spotify.exe"), 0.6)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "duck.json")))

    def test_recovery_leaves_a_volume_the_user_changed_since(self):
        d = make_ducker(self.mixer, self.tmp)
        d.acquire()
        self.mixer.apps["chrome.exe"][1] = 0.5      # the user turned it up themselves afterwards
        d2 = make_ducker(self.mixer, self.tmp)
        d2.recover()
        self.assertEqual(self.mixer.vol("chrome.exe"), 0.5)

    def test_disabled_is_a_no_op(self):
        d = AudioDucker(enabled=False, session_provider=self.mixer.sessions, journal_path=os.path.join(self.tmp, "j"))
        self.assertFalse(d.acquire())
        self.assertEqual(self.mixer.vol("chrome.exe"), 0.8)

    def test_missing_pycaw_degrades_quietly(self):
        def boom():
            raise ImportError("no pycaw")
        d = make_ducker(self.mixer, self.tmp)
        d._provider = boom
        self.assertFalse(d.acquire())
        self.assertFalse(d.enabled)          # stops trying
        d.release()                          # still harmless


class FakeDucker:
    def __init__(self):
        self.holds = 0
        self.acquired = 0

    def acquire(self):
        self.holds += 1
        self.acquired += 1
        return True

    def release(self):
        self.holds -= 1


def announce_rig(**cfg):
    cfg.setdefault("barge_in", True)    # the desktop app always runs with barge-in on (the mic is open while she talks)
    r = Rig(**cfg)
    r.fd = FakeDucker()
    r.v._ducker = r.fd
    return r


class AnnouncementTests(unittest.TestCase):
    def test_generation_starts_above_any_earlier_session(self):
        r = Rig()
        try:
            self.assertGreater(r.v._gen, 1_000_000_000)
        finally:
            r.close()

    def test_announcement_is_spoken_and_ducks_then_releases(self):
        r = announce_rig()
        try:
            r.v.announce("Reminder to turn off the laptop, sir.")
            self.assertEqual(r.fd.acquired, 1)
            r.wait(lambda: r.of("audio_chunk"))
            self.assertEqual(r.tts.texts[-1], "Reminder to turn off the laptop, sir.")
            r.wait(lambda: r.fd.holds == 0, timeout=4.0)       # released after the clip has played
        finally:
            r.close()

    def test_voice_mode_chatter_does_not_cut_a_reminder_off(self):
        r = announce_rig()
        try:
            r.v.set_voice_mode(True, announce=False)
            r.v.announce("Reminder to turn off the laptop, sir.")
            r.wait(lambda: r.of("audio_chunk"))
            r.say("and he goes around the outside of turn three")     # the race commentary
            self.assertEqual(r.submitted, [])
            self.assertEqual(r.of("audio_stop"), [])
            r.advance(5)                                              # the reminder has finished
            r.say("what time is it")
            self.assertEqual(r.submitted, ["what time is it"])
        finally:
            r.close()

    def test_stop_still_interrupts_a_reminder(self):
        r = announce_rig()
        try:
            r.v.set_voice_mode(True, announce=False)
            r.v.announce("Reminder to turn off the laptop, sir.")
            r.wait(lambda: r.of("audio_chunk"))
            r.say("stop")
            self.assertTrue(r.of("audio_stop"))
        finally:
            r.close()

    def test_wake_word_command_still_interrupts_a_reminder(self):
        r = announce_rig()
        try:
            r.v.announce("Reminder to turn off the laptop, sir.")
            r.wait(lambda: r.of("audio_chunk"))
            r.say("Hey Ember, what time is it?")
            self.assertEqual(r.submitted, ["what time is it?"])
            self.assertTrue(r.of("audio_stop"))
        finally:
            r.close()

    def test_a_new_reply_does_not_cut_a_reminder_but_still_cuts_a_reply(self):
        r = announce_rig()
        try:
            r.v.announce("Reminder to turn off the laptop, sir.")
            r.wait(lambda: r.of("audio_chunk"))
            r.v.begin_reply()
            self.assertEqual(r.of("audio_stop"), [])
            r.close()
            r = announce_rig()
            r.v._say("A long answer that is being read out.")
            r.wait(lambda: r.of("audio_chunk"))
            r.v.begin_reply()
            self.assertTrue(r.of("audio_stop"))
        finally:
            r.close()

    def test_incidental_speech_does_not_duck_a_reminder(self):
        r = announce_rig()
        try:
            r.v.announce("Reminder to turn off the laptop, sir.")
            r.wait(lambda: r.of("audio_chunk"))
            r.v.feed_audio(speech_bytes())
            time.sleep(0.3)
            self.assertEqual(r.of("voice_duck"), [])
            r.v.feed_audio(silence_bytes())          # that burst of speech ends
            # Control: once the reminder is over, the same sound ducks ordinary speech as before.
            r.advance(5)
            r.v._say("An ordinary reply.")
            r.wait(lambda: len(r.of("audio_chunk")) >= 2)
            r.v.feed_audio(speech_bytes())
            self.assertTrue(wait_for(lambda: r.of("voice_duck", on=True)))
        finally:
            r.close()

    def test_every_duck_is_released_even_when_queued_announcements_are_dropped(self):
        r = announce_rig()
        gate, entered = threading.Event(), threading.Event()
        real = r.tts.synthesize

        def slow(text, voice):
            entered.set()
            gate.wait(3.0)
            return real(text, voice)

        r.tts.synthesize = slow
        try:
            r.v.announce("First reminder, sir.")
            self.assertTrue(entered.wait(2.0))
            r.v.announce("Second reminder, sir.")          # still queued behind the first
            self.assertEqual(r.fd.acquired, 2)
            r.v.interrupt()                                 # "stop": drops the queued one, voids the first
            gate.set()
            r.wait(lambda: r.fd.holds == 0, timeout=4.0)
            self.assertFalse(r.v._announcing())
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
