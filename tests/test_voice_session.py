import base64
import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ember_voice import (  # noqa: E402
    PiperTTS, SileroVAD, UtteranceSegmenter, VoiceConfig, VoiceEngines, VoiceModelsMissing, VoiceSession,
    normalize_for_stt, voice_status,
)

FRAME = 512


class FakeVAD:
    def reset(self):
        pass

    def prob(self, frame):
        return 0.9 if float(np.abs(frame).max()) > 0.05 else 0.0


class FakeSTT:
    def __init__(self):
        self.script = []
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.script.pop(0) if self.script else ""


class FakeTTS:
    def __init__(self, seconds=0.3):
        self.texts = []
        self.seconds = seconds

    def synthesize(self, text, voice):
        self.texts.append(text)
        n = int(24000 * self.seconds)
        return (0.2 * np.sin(np.arange(n) * 0.05)).astype(np.float32), 24000


def speech_bytes(frames=12):
    rng = np.random.default_rng(0)
    return (rng.uniform(-0.5, 0.5, frames * FRAME) * 32767).astype("<i2").tobytes()


def silence_bytes(frames=30):
    return np.zeros(frames * FRAME, dtype="<i2").tobytes()


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Rig:
    def __init__(self, **cfg):
        self.msgs = []
        self.submitted = []
        self.cancelled = 0
        self.turn_running = False
        self.now = [1000.0]
        self.stt, self.tts = FakeSTT(), FakeTTS()
        engines = VoiceEngines(tts=self.tts, stt=self.stt, vad_factory=FakeVAD)
        config = VoiceConfig(debug=False, **cfg)
        self.v = VoiceSession(
            send=self.msgs.append,
            submit_turn=self._submit,
            cancel_turn=self._cancel,
            turn_active=lambda: self.turn_running,
            engines=engines, config=config, clock=lambda: self.now[0],
        )
        self.v.start()
        self.v.set_listening(True)

    def _submit(self, text):
        self.submitted.append(text)
        return True

    def _cancel(self):
        self.cancelled += 1
        self.turn_running = False

    def say(self, text):
        """Feed one utterance worth of audio; FakeSTT 'hears' `text`."""
        target = self.v._handled + 1
        self.stt.script.append(text)
        self.v.feed_audio(speech_bytes())
        self.v.feed_audio(silence_bytes())
        self.wait(lambda: self.v._handled >= target)

    def wait(self, cond, timeout=3.0):
        assert wait_for(cond, timeout), "timed out waiting"

    def of(self, type_, **match):
        return [m for m in self.msgs if m.get("type") == type_ and all(m.get(k) == v for k, v in match.items())]

    def reply(self, *deltas, final="", voice_origin=True):
        self.v.begin_reply()
        for d in deltas:
            self.v.feed_reply(d, "text")
        self.v.end_reply(final)

    def advance(self, seconds):
        self.now[0] += seconds
        self.v._refresh_state()

    def close(self):
        self.v.stop()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()

    def tearDown(self):
        self.r.close()

    def test_ambient_speech_is_ignored(self):
        self.r.say("the weather is nice today, isn't it")
        self.assertEqual(self.r.submitted, [])
        self.assertEqual(self.r.of("transcript"), [])

    def test_wake_command_reply_and_follow_up_window(self):
        r = self.r
        r.say("Hey Ember, what time is it?")
        self.assertEqual(r.submitted, ["what time is it?"])
        self.assertEqual(r.of("transcript")[0]["trigger"], "wake")

        r.reply("It's 6:30 p.m., sir. ", "Anything else?")
        r.wait(lambda: len(r.of("audio_chunk")) >= 2)
        self.assertEqual(r.tts.texts, ["It's 6:30 p.m., sir.", "Anything else?"])
        self.assertEqual(r.msgs and [m for m in r.of("voice_state")][-1]["state"], "speaking")
        chunk = r.of("audio_chunk")[0]
        self.assertEqual(chunk["sample_rate"], 24000)
        self.assertEqual(len(base64.b64decode(chunk["data"])), 24000 * 0.3 * 2)

        # follow-up window opens only once speech has finished
        self.assertFalse([m for m in r.of("voice_state")][-1]["awake"])
        r.advance(5.0)   # both chunks (0.6s) done
        st = [m for m in r.of("voice_state")][-1]
        self.assertEqual(st["state"], "listening")
        self.assertTrue(st["awake"])

        r.say("and tomorrow?")
        self.assertEqual(r.submitted[-1], "and tomorrow?")
        self.assertEqual(r.of("transcript")[-1]["trigger"], "follow_up")

        r.reply("Rain, sir.")
        r.wait(lambda: len(r.tts.texts) >= 3)
        r.advance(30.0)  # window long expired
        r.say("what about friday")
        self.assertEqual(len(r.submitted), 2)

    def test_bare_wake_acknowledges_and_opens_window(self):
        r = self.r
        r.say("Hey Ember.")
        r.wait(lambda: r.of("audio_chunk"))
        self.assertEqual(r.tts.texts, ["Sir?"])
        self.assertEqual(r.submitted, [])
        r.advance(2.0)   # "Sir?" finishes playing
        r.say("what's the weather")
        self.assertEqual(r.submitted, ["what's the weather"])
        self.assertEqual(r.of("transcript")[-1]["trigger"], "follow_up")

    def test_voice_mode_on_off(self):
        r = self.r
        r.say("Ember, voice mode on")
        self.assertTrue(r.of("voice_state")[-1]["voice_mode"])
        r.wait(lambda: r.tts.texts)
        self.assertIn("Voice mode on", r.tts.texts[0])
        self.assertEqual(r.submitted, [])

        r.advance(60)  # no window needed in voice mode
        r.say("what's the weather")
        self.assertEqual(r.submitted, ["what's the weather"])
        self.assertEqual(r.of("transcript")[-1]["trigger"], "voice_mode")

        r.say("that's all")
        self.assertFalse(r.of("voice_state")[-1]["voice_mode"])
        self.assertEqual(len(r.submitted), 1)
        r.advance(60)
        r.say("hello there friend")
        self.assertEqual(len(r.submitted), 1)

    def test_hud_toggle_of_voice_mode(self):
        r = self.r
        r.v.handle_control({"type": "voice", "action": "mode", "on": True})
        self.assertTrue(r.of("voice_state")[-1]["voice_mode"])
        self.assertEqual(r.tts.texts, [])     # HUD toggle is silent
        r.say("what's the weather")
        self.assertEqual(r.submitted, ["what's the weather"])

    def test_barge_in_interrupts_and_resubmits(self):
        r = self.r
        r.v.handle_control({"type": "voice", "action": "barge_in", "on": True})
        r.say("Ember, tell me a story")
        r.reply("Once upon a time there was a very long story indeed, sir.")
        r.wait(lambda: r.of("audio_chunk"))
        n_stops = len(r.of("audio_stop"))
        r.turn_running = True
        r.say("Ember, what's the date")
        self.assertGreater(len(r.of("audio_stop")), n_stops)
        self.assertEqual(r.cancelled, 1)
        self.assertEqual(r.submitted[-1], "what's the date")

    def test_stop_command_is_local(self):
        r = self.r
        r.v.handle_control({"type": "voice", "action": "barge_in", "on": True})
        r.say("Ember, tell me a story")
        r.reply("Once upon a time there was a very long story indeed, sir.")
        r.wait(lambda: r.of("audio_chunk"))
        r.turn_running = True
        r.say("Ember, stop")
        self.assertEqual(len(r.submitted), 1)          # "stop" never became a chat turn
        self.assertEqual(r.cancelled, 1)
        self.assertTrue(r.of("voice_event", event="stopped"))

    def test_stop_when_idle_is_ignored_but_flushes_client(self):
        r = self.r
        r.say("Ember, stop")
        self.assertEqual(r.submitted, [])   # nothing running: not sent to the LLM as a chat turn
        self.assertTrue(r.of("audio_stop"))   # but any lingering client audio is flushed

    def test_own_speech_echo_is_dropped_in_voice_mode(self):
        r = self.r
        r.v.handle_control({"type": "voice", "action": "barge_in", "on": True})
        r.v.set_voice_mode(True, announce=False)
        r.v._voice_turn_pending = True      # as if the previous turn had been spoken
        r.reply("Your calendar is clear until four, sir.")
        r.wait(lambda: r.tts.texts)
        r.say("your calendar is clear until four")
        self.assertEqual(r.submitted, [])
        r.say("what about tomorrow")
        self.assertEqual(r.submitted, ["what about tomorrow"])

    def test_status_command_keeps_ember_address(self):
        self.r.say("Ember, status.")
        self.assertEqual(self.r.submitted, ["Ember, status"])

    def test_interrupt_mutes_rest_of_reply(self):
        r = self.r
        r.v._voice_turn_pending = True
        r.v.begin_reply()
        r.v.feed_reply("First sentence here, sir. ", "text")
        r.wait(lambda: r.tts.texts)
        r.v.interrupt()
        before = list(r.tts.texts)
        r.v.feed_reply("Second sentence that should never be spoken. ", "text")
        r.v.end_reply("")
        time.sleep(0.2)
        self.assertEqual(r.tts.texts, before)
        self.assertTrue(r.of("audio_stop"))

    def test_typed_reply_spoken_only_with_speaker_on(self):
        r = self.r
        r.reply("A typed reply, sir.")
        time.sleep(0.2)
        self.assertEqual(r.tts.texts, [])
        r.v.handle_control({"type": "voice", "action": "speaker", "on": True})
        r.reply("Another typed reply, sir.")
        r.wait(lambda: r.tts.texts)
        self.assertEqual(r.tts.texts, ["Another typed reply, sir."])

    def test_final_text_spoken_when_nothing_streamed(self):
        r = self.r
        r.say("Ember, what time is it")
        r.v.begin_reply()          # local intents return a full string with no stream deltas
        r.v.end_reply("It's 6:30 PM, sir.")
        r.wait(lambda: r.tts.texts)
        self.assertEqual(r.tts.texts, ["It's 6:30 PM, sir."])

    def test_status_note_spoken_once(self):
        r = self.r
        r.say("Ember, who won the race")
        r.v.begin_reply()
        r.v.feed_reply("Searching for that, sir — one moment...", "status")
        r.v.feed_reply("Checking sources, sir.", "status")
        r.wait(lambda: r.tts.texts)
        time.sleep(0.1)
        self.assertEqual(len(r.tts.texts), 1)

    def test_not_listening_ignores_audio(self):
        r = self.r
        r.v.set_listening(False)
        r.stt.script.append("Hey Ember what time is it")
        r.v.feed_audio(speech_bytes())
        r.v.feed_audio(silence_bytes())
        time.sleep(0.3)
        self.assertEqual(r.stt.calls, 0)

    def test_duck_then_restore_when_not_for_ember(self):
        r = self.r
        r.v.handle_control({"type": "voice", "action": "barge_in", "on": True})
        r.say("Ember, tell me a story")
        r.reply("Once upon a time there was a very long story indeed, sir.")
        r.wait(lambda: r.of("audio_chunk"))
        r.say("just chatting with someone else here")
        self.assertTrue(r.of("voice_duck", on=True))
        self.assertTrue(r.of("voice_duck", on=False))
        self.assertEqual(len(r.submitted), 1)


class HalfDuplexAndDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()

    def tearDown(self):
        self.r.close()

    def test_mic_dropped_while_ember_speaks_then_resumes(self):
        r = self.r
        r.say("Ember, tell me a story")
        r.reply("Once upon a time there was a very long story indeed, sir.")
        r.wait(lambda: r.of("audio_chunk"))
        calls = r.stt.calls
        r.stt.script.append("Ember, what was that")     # would be accepted if it were heard
        r.v.feed_audio(speech_bytes())
        r.v.feed_audio(silence_bytes())
        time.sleep(0.4)
        self.assertEqual(r.stt.calls, calls)             # never transcribed: mic was deaf
        self.assertEqual(len(r.submitted), 1)
        r.stt.script.clear()
        r.advance(5.0)                                   # she has finished speaking
        r.say("and another thing")                       # follow-up window is open
        self.assertEqual(r.submitted[-1], "and another thing")

    def test_barge_in_control_toggles_listening_during_speech(self):
        r = self.r
        self.assertFalse(r.v.config.barge_in)
        r.v.handle_control({"type": "voice", "action": "barge_in", "on": True})
        self.assertTrue(r.v.config.barge_in)

    def test_heard_stream_shows_what_was_ignored(self):
        r = self.r
        r.v.handle_control({"type": "voice", "action": "debug", "on": True})
        r.say("hey amber what's up")                    # alias accepted
        r.say("nothing to do with anyone here")
        heard = r.of("voice_heard")
        self.assertEqual(heard[0]["verdict"], "accepted (wake)")
        self.assertEqual(heard[1]["verdict"], "ignored (no wake phrase)")
        self.assertEqual(heard[1]["text"], "nothing to do with anyone here")

    def test_heard_reports_loudness_and_saves_clips(self):
        import tempfile
        import wave
        r = self.r
        r.v.handle_control({"type": "voice", "action": "debug", "on": True})
        with tempfile.TemporaryDirectory() as d:
            os.environ["EMBER_VOICE_SAVE_CLIPS"] = d
            try:
                r.say("nothing to do with anyone here")
            finally:
                del os.environ["EMBER_VOICE_SAVE_CLIPS"]
            files = os.listdir(d)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith("ignored_no_wake_phrase.wav"), files)
            with wave.open(os.path.join(d, files[0])) as w:
                self.assertEqual(w.getframerate(), 16000)
                self.assertGreater(w.getnframes(), 16000 * 0.3)
        heard = r.of("voice_heard")[0]
        self.assertGreater(heard["peak"], 0.2)
        self.assertGreater(heard["level_db"], -30)
        self.assertGreater(heard["speech_ratio"], 0.4)

    def test_latency_breakdown_sent_once_per_voice_turn(self):
        r = self.r
        r.say("Ember, what time is it")
        r.v.begin_reply()
        r.v.feed_reply("It is half past six, sir. ", "text")
        r.v.end_reply("")
        r.wait(lambda: r.of("voice_timing"))
        t = r.of("voice_timing")[0]
        for key in ("endpoint_s", "stt_s", "turn_start_s", "first_text_s", "tts_s", "total_s"):
            self.assertIn(key, t)
        r.v.begin_reply()
        r.v.feed_reply("Typed follow up, sir. ", "text")
        r.v.end_reply("")
        time.sleep(0.2)
        self.assertEqual(len(r.of("voice_timing")), 1)   # typed replies aren't timed


class SlowTTS(FakeTTS):
    """Takes longer to synthesize than the audio lasts (real-time factor > 1)."""

    def synthesize(self, text, voice):
        time.sleep(0.7)
        self.texts.append(text)
        n = int(24000 * 0.6)
        return (0.2 * np.sin(np.arange(n) * 0.05)).astype(np.float32), 24000


class EngineHelperTests(unittest.TestCase):
    def test_normalize_for_stt_lifts_quiet_audio_only(self):
        quiet = (0.05 * np.sin(np.arange(16000) * 0.1)).astype(np.float32)
        out = normalize_for_stt(quiet)
        self.assertAlmostEqual(float(np.abs(out).max()), 0.5, delta=0.02)          # x10 cap: 0.05 -> 0.5
        loud = (0.8 * np.sin(np.arange(16000) * 0.1)).astype(np.float32)
        self.assertTrue(np.array_equal(normalize_for_stt(loud), loud))               # never attenuates
        silent = np.zeros(1600, dtype=np.float32)
        self.assertEqual(float(np.abs(normalize_for_stt(silent)).max()), 0.0)        # never amplifies pure silence
        self.assertEqual(normalize_for_stt(np.zeros(0, dtype=np.float32)).size, 0)

    def test_voice_status_shape(self):
        ok, msg = voice_status()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.startswith("ready") or msg.startswith("missing"))

    def test_piper_missing_voices_is_a_clear_error(self):
        with self.assertRaises(VoiceModelsMissing):
            PiperTTS(voices_dir="/nonexistent/dir").synthesize("hello there", "daniel")


class SlowTTSWarningTests(unittest.TestCase):
    def test_warns_once_when_synthesis_is_slower_than_realtime(self):
        r = Rig()
        r.tts = SlowTTS()
        r.v._tts = r.tts
        try:
            r.v.handle_control({"type": "voice", "action": "speaker", "on": True})
            r.reply("First sentence here, sir. ", "Second sentence follows it, sir. ", "Third one closes it, sir.")
            r.wait(lambda: r.of("voice_warning"), timeout=10)
            time.sleep(1.0)
            self.assertEqual(len(r.of("voice_warning")), 1)
            self.assertIn("slower than real time", r.of("voice_warning")[0]["text"])
            self.assertIn("EMBER_TTS=piper", r.of("voice_warning")[0]["text"])
        finally:
            r.close()


class SegmenterTests(unittest.TestCase):
    def collect(self, pcm, chunk=700):
        seg = UtteranceSegmenter(FakeVAD(), VoiceConfig(debug=False))
        events = []
        for i in range(0, len(pcm), chunk * 2):
            events += seg.feed(pcm[i:i + chunk * 2])
        return events

    def test_one_utterance(self):
        events = self.collect(silence_bytes(10) + speech_bytes(20) + silence_bytes(40))
        self.assertEqual([e.kind for e in events], ["start", "end"])
        self.assertIsNotNone(events[1].audio)

    def test_short_burst_discarded_but_end_reported(self):
        events = self.collect(silence_bytes(10) + speech_bytes(4) + silence_bytes(40))
        self.assertEqual([e.kind for e in events], ["start", "end"])
        self.assertIsNone(events[1].audio)

    def test_pause_shorter_than_endpoint_stays_one_utterance(self):
        events = self.collect(speech_bytes(12) + silence_bytes(10) + speech_bytes(12) + silence_bytes(40))
        self.assertEqual([e.kind for e in events], ["start", "end"])

    def test_two_utterances(self):
        events = self.collect(speech_bytes(12) + silence_bytes(40) + speech_bytes(12) + silence_bytes(40))
        self.assertEqual([e.kind for e in events], ["start", "end", "start", "end"])

    def test_max_length_forces_end(self):
        cfg = VoiceConfig(debug=False, max_utterance_s=1.0)
        seg = UtteranceSegmenter(FakeVAD(), cfg)
        events = seg.feed(speech_bytes(100))
        self.assertIn("end", [e.kind for e in events])


PIPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "piper")
PIPER_MAP = {"daniel": "en-gb-alan-low"}
HAVE_PIPER = os.path.exists(os.path.join(PIPER_DIR, "en-gb-alan-low.onnx")) and os.path.exists(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "silero_vad.onnx"))


@unittest.skipUnless(HAVE_PIPER, "piper test voice not downloaded")
class PiperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tts = PiperTTS(voices_dir=PIPER_DIR, voice_map=PIPER_MAP)

    def test_synthesize_shape_level_and_speed(self):
        text = "It is half past six, sir, and your evening is otherwise clear."
        self.tts.synthesize("warm up please", "daniel")
        t0 = time.perf_counter()
        samples, sr = self.tts.synthesize(text, "daniel")
        dt = time.perf_counter() - t0
        self.assertEqual(samples.dtype, np.float32)
        self.assertEqual(sr, 16000)
        dur = len(samples) / sr
        self.assertGreater(dur, 2.0)
        self.assertLessEqual(float(np.abs(samples).max()), 0.9001)          # headroom left
        self.assertLess(dt / dur, 0.5, f"real-time factor {dt / dur:.2f}")

    def test_unknown_voice_falls_back_to_an_installed_one(self):
        samples, sr = self.tts.synthesize("Good evening, sir.", "heart")
        self.assertGreater(len(samples), 8000)

    def test_short_phrases_are_cached(self):
        a = self.tts.synthesize("Sir?", "daniel")
        b = self.tts.synthesize("Sir?", "daniel")
        self.assertIs(a[0], b[0])

    def test_full_session_with_piper_and_real_vad(self):
        msgs, submitted = [], []
        stt = FakeSTT()
        stt.script = ["Hey Ember, what time is it?"]
        engines = VoiceEngines(tts=self.tts, stt=stt,
                               vad_factory=lambda: SileroVAD(os.path.join(PIPER_DIR, "..", "silero_vad.onnx")))
        v = VoiceSession(msgs.append, lambda t: submitted.append(t) or True, lambda: None, lambda: False,
                         engines=engines, config=VoiceConfig(debug=False))
        v.start()
        v.set_listening(True)
        s, sr = self.tts.synthesize("Hey Ember, what time is it?", "daniel")
        pcm = to_16k_pcm(s, sr)
        raw = np.concatenate([np.zeros(8000, dtype="<i2"), pcm, np.zeros(24000, dtype="<i2")]).tobytes()
        for i in range(0, len(raw), 1600):
            v.feed_audio(raw[i:i + 1600])
        self.assertTrue(wait_for(lambda: submitted, 10))
        v.begin_reply()
        v.feed_reply("It is half past six, sir. ", "text")
        v.feed_reply("Your evening is otherwise clear, sir.", "text")
        v.end_reply("")
        self.assertTrue(wait_for(lambda: [m for m in msgs if m["type"] == "voice_timing"], 10))
        chunks = [m for m in msgs if m["type"] == "audio_chunk"]
        self.assertEqual(chunks[0]["sample_rate"], 16000)
        timing = [m for m in msgs if m["type"] == "voice_timing"][0]
        self.assertLess(timing["tts_s"], 1.0, timing)     # first audio well under a second after first text
        v.stop()


# ---------------------------------------------------------------------------
# Integration with the REAL Silero + Kokoro models (skipped if not present)
# ---------------------------------------------------------------------------
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _first_existing(*paths):
    return next((p for p in paths if os.path.exists(p)), paths[0])


# data/voice_models is where fetch_voice_models.py puts things in the real project
_DM = os.path.join(ROOT, "data", "voice_models")
KOKORO = _first_existing(os.path.join(_DM, "kokoro-v1.0.onnx"), os.path.join(ROOT, "models", "kokoro-fp32.onnx"))
VOICES = _first_existing(os.path.join(_DM, "voices-v1.0.bin"), os.path.join(ROOT, "models", "voices.bin"))
SILERO = _first_existing(os.path.join(_DM, "silero_vad.onnx"), os.path.join(ROOT, "models", "silero_vad.onnx"))
HAVE_MODELS = all(os.path.exists(p) for p in (KOKORO, VOICES, SILERO))


def to_16k_pcm(samples, sr):
    n = int(len(samples) * 16000 / sr)
    x = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    return (x * 32767).astype("<i2")


@unittest.skipUnless(HAVE_MODELS, "voice models not downloaded")
class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ember_voice import KokoroTTS
        cls.tts = KokoroTTS(KOKORO, VOICES)
        cls.speech = {}
        for key, text in (("a", "Hey Ember, what time is it?"), ("b", "Good evening, sir. Your calendar is clear until four.")):
            s, sr = cls.tts.synthesize(text, "daniel")
            cls.speech[key] = to_16k_pcm(s, sr)

    def segs(self, pcm):
        seg = UtteranceSegmenter(SileroVAD(SILERO), VoiceConfig(debug=False))
        raw = pcm.tobytes()
        out = []
        for i in range(0, len(raw), 1600):     # 50 ms client-style frames, not VAD-aligned
            out += seg.feed(raw[i:i + 1600])
        return out

    def sil(self, sec):
        return np.zeros(int(16000 * sec), dtype="<i2")

    def test_single_utterance_detected(self):
        pcm = np.concatenate([self.sil(1.0), self.speech["a"], self.sil(1.5)])
        ev = self.segs(pcm)
        self.assertEqual([e.kind for e in ev], ["start", "end"])
        dur = len(self.speech["a"]) / 16000
        got = len(ev[1].audio) / 16000
        self.assertGreater(got, dur * 0.9)
        self.assertLess(got, dur + 1.0)

    def test_silence_and_noise_ignored(self):
        rng = np.random.default_rng(1)
        noise = (rng.normal(0, 300, 16000 * 3)).astype("<i2")
        self.assertEqual(self.segs(np.concatenate([self.sil(2.0), noise, self.sil(1.0)])), [])

    def test_long_pause_splits_short_pause_joins(self):
        a, b = self.speech["a"], self.speech["b"]
        split = self.segs(np.concatenate([a, self.sil(1.6), b, self.sil(1.5)]))
        self.assertEqual([e.kind for e in split], ["start", "end", "start", "end"])
        joined = self.segs(np.concatenate([a, self.sil(0.35), b, self.sil(1.5)]))
        self.assertEqual([e.kind for e in joined], ["start", "end"])

    def test_full_session_with_real_vad_and_tts(self):
        msgs, submitted = [], []
        stt = FakeSTT()
        stt.script = ["Hey Ember, what time is it?"]
        engines = VoiceEngines(tts=self.tts, stt=stt, vad_factory=lambda: SileroVAD(SILERO))
        v = VoiceSession(msgs.append, lambda t: submitted.append(t) or True, lambda: None, lambda: False,
                         engines=engines, config=VoiceConfig(debug=False))
        v.start()
        v.set_listening(True)
        raw = np.concatenate([self.sil(0.5), self.speech["a"], self.sil(1.5)]).tobytes()
        for i in range(0, len(raw), 1600):
            v.feed_audio(raw[i:i + 1600])
        self.assertTrue(wait_for(lambda: submitted, 10))
        self.assertEqual(submitted, ["what time is it?"])

        v.begin_reply()
        v.feed_reply("It is half past six, sir. ", "text")
        v.feed_reply("Your evening is otherwise clear.", "text")
        v.end_reply("")
        self.assertTrue(wait_for(lambda: len([m for m in msgs if m["type"] == "audio_chunk"]) >= 2, 30))
        chunks = [m for m in msgs if m["type"] == "audio_chunk"]
        pcm = np.concatenate([np.frombuffer(base64.b64decode(m["data"]), dtype="<i2") for m in chunks])
        self.assertGreater(len(pcm) / chunks[0]["sample_rate"], 2.0)   # two real sentences of speech
        self.assertGreater(np.abs(pcm).max(), 2000)
        self.assertEqual([m["seq"] for m in chunks], sorted(m["seq"] for m in chunks))
        v.stop()


if __name__ == "__main__":
    unittest.main()
