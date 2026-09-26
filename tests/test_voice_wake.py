"""
Acoustic wake word ("hey ember"): absolute sample positions in the segmenter, the alignment between
wake detections and utterances, slicing the audio Whisper sees, bare-wake handling, and the engine
selection fix (VoiceEngines must actually pick Piper). Plus real-model end-to-end tests that are
skipped when the models aren't downloaded.

Run from the project root:  python -m unittest tests.test_voice_wake
"""
import os
import sys
import time
import unittest
from unittest import mock

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import ember_voice  # noqa: E402
from ember_voice import (  # noqa: E402
    PiperTTS, SileroVAD, UtteranceSegmenter, VoiceConfig, VoiceEngines, VoiceSession, WakeSpotter,
)
from ember_voice_text import soft_wake_vetoed, strip_wake_residue  # noqa: E402
from tests.test_voice_session import FakeTTS, FakeVAD, silence_bytes, speech_bytes, wait_for  # noqa: E402

FRAME = 512
SR = 16000


class RecordingSTT:
    """Returns scripted text and remembers exactly which audio it was given."""

    def __init__(self, *texts):
        self.script = list(texts)
        self.audios = []
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        self.audios.append(np.array(audio))
        return self.script.pop(0) if self.script else ""


class FakeWakeStream:
    """Fires at absolute sample positions (reported, like the real one, as 'samples fed so far').
    `fire_at` items are a position (meaning "hey_ember") or a (position, keyword_name) pair."""

    def __init__(self, fire_at):
        self.fire_at = sorted(((f, "hey_ember") if isinstance(f, int) else f for f in fire_at), key=lambda x: x[0])
        self.samples_fed = 0

    def accept(self, pcm16):
        self.samples_fed += len(pcm16) // 2
        hits = []
        while self.fire_at and self.samples_fed >= self.fire_at[0][0]:
            _, name = self.fire_at.pop(0)
            hits.append((self.samples_fed, name))
        return hits


class FakeSpotter:
    def __init__(self, fire_at):
        self.fire_at = fire_at

    def new_stream(self):
        return FakeWakeStream(self.fire_at)


def feed_in_chunks(session, pcm: bytes, chunk_samples: int = 640):
    step = chunk_samples * 2
    for i in range(0, len(pcm), step):
        session.feed_audio(pcm[i:i + step])


# Timeline used by the fake-detector tests (in samples):
#   20 silent frames | 30 speech frames | 30 silent frames
SPEECH_START = 20 * FRAME                # 10240
SPEECH_END = 50 * FRAME                  # 25600
SEG_START = SPEECH_START - 8 * FRAME     # segmenter keeps a 10-frame pre-roll (incl. the frame that triggered it)


def stream_bytes():
    return silence_bytes(20) + speech_bytes(30) + silence_bytes(30)


class WakeRig:
    def __init__(self, fire_at, stt):
        self.msgs, self.submitted = [], []
        self.stt, self.tts = stt, FakeTTS()
        self.now = [1000.0]
        engines = VoiceEngines(tts=self.tts, stt=stt, vad_factory=FakeVAD, wake=FakeSpotter(fire_at))
        self.v = VoiceSession(self.msgs.append, lambda t: self.submitted.append(t) or True, lambda: None, lambda: False,
                              engines=engines, config=VoiceConfig(debug=False), clock=lambda: self.now[0])
        self.v.start()
        self.v.set_listening(True)

    def play(self, pcm=None, expect_handled=1):
        feed_in_chunks(self.v, pcm if pcm is not None else stream_bytes())
        assert wait_for(lambda: self.v._handled >= expect_handled, 5), "utterance was never handled"
        time.sleep(0.05)

    def of(self, type_, **match):
        return [m for m in self.msgs if m.get("type") == type_ and all(m.get(k) == v for k, v in match.items())]

    def close(self):
        self.v.stop()


class SegmenterPositionTests(unittest.TestCase):
    def test_start_sample_is_absolute_for_any_chunking(self):
        pcm = silence_bytes(20) + speech_bytes(30) + silence_bytes(30)
        for chunk_samples in (512, 640, 700, 1000, 3333):
            seg = UtteranceSegmenter(FakeVAD(), VoiceConfig(debug=False))
            events = []
            for i in range(0, len(pcm), chunk_samples * 2):
                events += seg.feed(pcm[i:i + chunk_samples * 2])
            end = [e for e in events if e.kind == "end"][0]
            self.assertEqual(end.start_sample, SEG_START, f"chunk {chunk_samples}")
            # audio[0] really is at that absolute position: the first speech sample sits where expected
            first_speech = int(np.argmax(np.abs(end.audio) > 0.05))
            self.assertEqual(end.start_sample + first_speech, SPEECH_START, f"chunk {chunk_samples}")

    def test_positions_stay_correct_after_a_reset(self):
        seg = UtteranceSegmenter(FakeVAD(), VoiceConfig(debug=False))
        seg.feed(silence_bytes(7) + b"\x00\x00" * 100)      # odd leftovers in the carry
        before = seg.samples_fed
        seg.reset()                                         # e.g. the half-duplex mute
        events = seg.feed(silence_bytes(20) + speech_bytes(30) + silence_bytes(30))
        end = [e for e in events if e.kind == "end"][0]
        first_speech = int(np.argmax(np.abs(end.audio) > 0.05))
        self.assertEqual(end.start_sample + first_speech, before + SPEECH_START)


class SoftKeywordTests(unittest.TestCase):
    """Bare "Ember" / "wake up" only count when they come FIRST in the utterance."""

    EARLY = SPEECH_START + 6 * FRAME          # ~0.2 s into the speech
    LATE = SPEECH_START + 28 * FRAME          # ~0.9 s in... still inside the 1.8 s window relative to the segment start?

    def run_case(self, hits, stt_text="what time is it"):
        # a soft hit costs a short "head" pass first (to veto look-alike words), then the command pass;
        # strong hits and no-hit utterances need only the one pass
        soft = any(name != "hey_ember" for _, name in (h if isinstance(h, tuple) else (h, "hey_ember") for h in hits))
        stt = RecordingSTT(*(["Humber", stt_text] if soft else [stt_text]))
        r = WakeRig(hits, stt)
        try:
            # a long utterance (3 s of speech) so a "late" position is genuinely late
            r.play(pcm=silence_bytes(20) + speech_bytes(94) + silence_bytes(30))
            return r.submitted[:], stt.calls
        finally:
            r.close()

    def test_soft_hit_at_the_start_wakes_her(self):
        submitted, calls = self.run_case([(SPEECH_START + 6 * FRAME, "ember")])
        self.assertEqual(submitted, ["what time is it"])

    def test_soft_hit_in_the_middle_of_a_sentence_does_not(self):
        late = SPEECH_START + 80 * FRAME                      # ~2.5 s into a 3 s utterance
        submitted, calls = self.run_case([(late, "ember")])
        self.assertEqual(submitted, [])                        # falls back to the transcript path -> no wake phrase
        self.assertEqual(calls, 1)

    def test_strong_hit_counts_anywhere(self):
        late = SPEECH_START + 80 * FRAME
        submitted, _ = self.run_case([(late, "hey_ember")])
        self.assertEqual(submitted, ["what time is it"])

    def test_strong_is_preferred_when_both_fired(self):
        # the soft hit is early, the strong one is later: the audio must be cut after the STRONG one
        stt = RecordingSTT("what time is it")
        early_soft, strong = SPEECH_START + 6 * FRAME, SPEECH_START + 40 * FRAME
        r = WakeRig([(early_soft, "ember"), (strong, "hey_ember")], stt)
        try:
            r.play(pcm=silence_bytes(20) + speech_bytes(94) + silence_bytes(30))
            self.assertEqual(r.submitted, ["what time is it"])
            first_sample_abs = strong - int(0.35 * SR)
            full_start = SEG_START
            self.assertGreater(first_sample_abs - full_start, 0)
            # 94 speech frames + tail: audio handed to Whisper starts at the strong hit, not the early soft one
            self.assertLess(len(stt.audios[0]), (94 * FRAME + 7 * FRAME + 8 * FRAME) - (early_soft - SEG_START) + 1)
        finally:
            r.close()

    def test_look_alike_word_is_vetoed_then_handled_as_ordinary_speech(self):
        stt = RecordingSTT("Remember to buy", "remember to buy milk")     # head pass, then the normal full pass
        r = WakeRig([(SPEECH_START + 6 * FRAME, "ember")], stt)
        try:
            r.play(pcm=silence_bytes(20) + speech_bytes(94) + silence_bytes(30))
            self.assertEqual(r.submitted, [])                            # no wake phrase in the transcript either
            self.assertEqual(stt.calls, 2)
            self.assertLess(len(stt.audios[0]), len(stt.audios[1]))       # the first pass looked at just the start
        finally:
            r.close()

    def test_unknown_word_at_the_start_is_accepted_and_the_command_is_cut_after_it(self):
        stt = RecordingSTT("Humber", "which is the next race weekend")    # head pass, then the command
        r = WakeRig([(SPEECH_START + 6 * FRAME, "ember")], stt)
        try:
            r.play(pcm=silence_bytes(20) + speech_bytes(94) + silence_bytes(30))
            self.assertEqual(r.submitted, ["which is the next race weekend"])
            self.assertEqual(stt.calls, 2)
            self.assertEqual(r.of("transcript")[0]["trigger"], "wake")
        finally:
            r.close()

    def test_bare_soft_wake_answers_sir(self):
        stt = RecordingSTT("Ember")                                       # only the head pass is ever needed
        r = WakeRig([(SPEECH_END - 2 * FRAME, "ember")], stt)
        try:
            r.play()
            self.assertEqual(stt.calls, 1)
            self.assertTrue(wait_for(lambda: r.tts.texts, 3))
            self.assertEqual(r.tts.texts, ["Sir?"])
        finally:
            r.close()

    def test_soft_keyword_can_be_switched_off(self):
        with mock.patch.object(ember_voice, "KWS_STRONG", ("hey_ember", "ember", "wake_up")):
            pass    # (the switch itself is EMBER_KWS_SOFT=0, which stops the soft phrases being loaded at all)
        text = open(ember_voice.__file__, encoding="utf-8").read()
        self.assertIn('KWS_SOFT_ENABLED = os.environ.get("EMBER_KWS_SOFT", "1")', text)


class VetoTests(unittest.TestCase):
    def test_vetoed_and_not(self):
        for t in ("Remember", "remember to buy", "December is", "the month of", "Members of", "Timber!", "Embed this"):
            self.assertEqual(soft_wake_vetoed(t), t.split()[0].lower().strip("!,.") in
                             {"remember", "december", "members", "timber", "embed"}, t)
        for t in ("Humber", "Amber", "M boy", "Enbow", "Ember", "", "wake up"):
            self.assertFalse(soft_wake_vetoed(t), t)

    def test_env_extends_the_list(self):
        with mock.patch.dict(os.environ, {"EMBER_WAKE_SOFT_VETO": "number, tender"}):
            self.assertTrue(soft_wake_vetoed("Number seven"))
            self.assertTrue(soft_wake_vetoed("tender loving"))


class ResidueTests(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(strip_wake_residue("Ember, what time is it?"), "what time is it?")
        self.assertEqual(strip_wake_residue("M. What can you tell me"), "What can you tell me")
        self.assertEqual(strip_wake_residue("Hey, Ember what's up"), "what's up")
        self.assertEqual(strip_wake_residue("what time is it"), "what time is it")
        self.assertEqual(strip_wake_residue("hey"), "hey")                       # never strips the last word
        self.assertEqual(strip_wake_residue("um uh hey em okay what now"), "em okay what now")  # at most three tokens


class AcousticWakeTests(unittest.TestCase):
    def test_command_after_wake_word_transcribes_only_the_command_audio(self):
        wake_at = SPEECH_START + 12 * FRAME + 800               # keyword "ends" 12 frames into the speech
        stt = RecordingSTT("what time is it")                  # NO wake word in the transcript
        r = WakeRig([wake_at], stt)
        try:
            r.play()
            self.assertEqual(r.submitted, ["what time is it"])
            self.assertEqual(r.of("transcript")[0]["trigger"], "wake")
            self.assertTrue(r.of("voice_event", event="wake"))
            self.assertEqual(stt.calls, 1)
            heard = stt.audios[0]
            # the audio cut starts 0.35 s before the detection point, which is inside the speech...
            expected_first = wake_at - int(0.35 * SR)
            self.assertGreater(expected_first, SPEECH_START)
            self.assertGreater(float(np.abs(heard[:200]).max()), 0.05)
            # ...whereas the full utterance would have started with the silent pre-roll
            full_len = len(heard) + (expected_first - SEG_START)
            self.assertGreater(full_len, len(heard))
        finally:
            r.close()

    def test_whisper_residue_of_the_name_is_stripped(self):
        r = WakeRig([SPEECH_START + 12 * FRAME], RecordingSTT("M. What time is it?"))
        try:
            r.play()
            self.assertEqual(r.submitted, ["What time is it?"])
        finally:
            r.close()

    def test_bare_hey_ember_skips_whisper_and_answers_sir(self):
        near_the_end = SPEECH_END - 2 * FRAME                   # detection right at the end of the speech
        stt = RecordingSTT("should never be asked")
        r = WakeRig([near_the_end], stt)
        try:
            r.play()
            self.assertEqual(stt.calls, 0)                       # no transcription pass at all
            self.assertEqual(r.submitted, [])
            self.assertTrue(wait_for(lambda: r.tts.texts, 3))
            self.assertEqual(r.tts.texts, ["Sir?"])
            self.assertTrue(r.of("voice_event", event="wake"))
        finally:
            r.close()

    def test_stale_detection_from_before_the_utterance_is_ignored(self):
        stt = RecordingSTT("what time is it")
        r = WakeRig([100], stt)                                  # fired long before this utterance began
        try:
            r.play()
            self.assertEqual(stt.calls, 1)                       # normal path: transcribed in full...
            self.assertEqual(r.submitted, [])                    # ...and ignored: no wake phrase in the text
        finally:
            r.close()

    def test_one_detection_wakes_one_utterance_only(self):
        stt = RecordingSTT("what time is it", "and what about tomorrow")
        r = WakeRig([SPEECH_START + 12 * FRAME], stt)
        try:
            r.play(expect_handled=1)
            self.assertEqual(r.submitted, ["what time is it"])
            r.now[0] += 60                                       # follow-up window long over
            r.play(pcm=speech_bytes(30) + silence_bytes(30), expect_handled=2)
            self.assertEqual(r.submitted, ["what time is it"])   # second utterance had no detection -> ignored
        finally:
            r.close()

    def test_no_wake_stream_means_the_old_transcript_path(self):
        stt = RecordingSTT("Hey Ember, what time is it?")
        engines = VoiceEngines(tts=FakeTTS(), stt=stt, vad_factory=FakeVAD)          # no wake=
        self.assertIsNone(engines.new_wake_stream())
        submitted = []
        v = VoiceSession(lambda m: None, lambda t: submitted.append(t) or True, lambda: None, lambda: False,
                         engines=engines, config=VoiceConfig(debug=False))
        v.start()
        v.set_listening(True)
        try:
            feed_in_chunks(v, stream_bytes())
            self.assertTrue(wait_for(lambda: submitted, 5))
            self.assertEqual(submitted, ["what time is it?"])
        finally:
            v.stop()

    def test_stopping_listening_discards_pending_detections(self):
        r = WakeRig([SPEECH_START + 12 * FRAME], RecordingSTT("what time is it"))
        try:
            feed_in_chunks(r.v, silence_bytes(20) + speech_bytes(14))
            time.sleep(0.2)
            r.v.set_listening(False)
            self.assertEqual(len(r.v._wake_hits), 0)
        finally:
            r.close()


class EngineSelectionTests(unittest.TestCase):
    """Regression: VoiceEngines() must go through make_tts(). It used to construct Kokoro directly,
    so installing Piper changed nothing."""

    def test_default_engine_follows_make_tts(self):
        with mock.patch.object(ember_voice, "make_tts", return_value=FakeTTS()) as mk:
            eng = VoiceEngines(stt=RecordingSTT(), vad_factory=FakeVAD)
        mk.assert_called_once()
        self.assertIsInstance(eng.tts, FakeTTS)

    def test_auto_picks_piper_when_ready_kokoro_otherwise(self):
        with mock.patch.object(ember_voice, "TTS_ENGINE", "auto"), mock.patch.object(ember_voice, "piper_ready", return_value=True):
            self.assertEqual(ember_voice.make_tts().name, "piper")
        with mock.patch.object(ember_voice, "TTS_ENGINE", "auto"), mock.patch.object(ember_voice, "piper_ready", return_value=False):
            self.assertEqual(ember_voice.make_tts().name, "kokoro")
        with mock.patch.object(ember_voice, "TTS_ENGINE", "kokoro"), mock.patch.object(ember_voice, "piper_ready", return_value=True):
            self.assertEqual(ember_voice.make_tts().name, "kokoro")
        with mock.patch.object(ember_voice, "TTS_ENGINE", "piper"):
            self.assertEqual(ember_voice.make_tts().name, "piper")


# ---------------------------------------------------------------------------
# Real models (skipped when not downloaded)
# ---------------------------------------------------------------------------
def _first_dir(*candidates):
    for c in candidates:
        if all(os.path.exists(os.path.join(c, f)) for f in ember_voice.KWS_FILES.values()):
            return c
    return None


KWS_DIR = _first_dir(
    os.path.join(ROOT, "data", "voice_models", "kws"),
    os.path.join(ROOT, "models", "kws", "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"),
)
_SILERO = next((p for p in (os.path.join(ROOT, "data", "voice_models", "silero_vad.onnx"),
                            os.path.join(ROOT, "models", "silero_vad.onnx")) if os.path.exists(p)), None)
_KOKORO = next(((m, v) for m, v in (
    (os.path.join(ROOT, "data", "voice_models", "kokoro-v1.0.onnx"), os.path.join(ROOT, "data", "voice_models", "voices-v1.0.bin")),
    (os.path.join(ROOT, "models", "kokoro-fp32.onnx"), os.path.join(ROOT, "models", "voices.bin")),
) if os.path.exists(m) and os.path.exists(v)), None)
HAVE_REAL = bool(KWS_DIR and _SILERO and _KOKORO and ember_voice._module_available("sherpa_onnx")
                 and ember_voice._module_available("kokoro_onnx"))


@unittest.skipUnless(HAVE_REAL, "wake-word / VAD / Kokoro models not downloaded")
class RealWakeWordTests(unittest.TestCase):
    """Real Silero VAD + real keyword spotter + real synthesized speech. The fake Whisper never says
    the wake word, so a command can only get through if the ACOUSTIC detector heard 'hey ember'."""

    VOICES = (("bm_daniel", "en-gb"), ("af_heart", "en-us"), ("am_adam", "en-us"), ("bf_isabella", "en-gb"))

    @classmethod
    def setUpClass(cls):
        cls.spotter = WakeSpotter(model_dir=KWS_DIR)
        cls.tts = ember_voice.KokoroTTS(*_KOKORO)

    def speak(self, text, voice, lang):
        samples, sr = self.tts._load().create(text, voice=voice, speed=1.0, lang=lang)
        x = np.interp(np.linspace(0, len(samples) - 1, int(len(samples) * SR / sr)), np.arange(len(samples)), samples)
        return np.concatenate([np.zeros(12000, "<i2"), (x * 32767).astype("<i2"), np.zeros(24000, "<i2")]).tobytes()

    def run_session(self, pcm, stt_text):
        stt = RecordingSTT(stt_text)
        msgs, submitted = [], []
        eng = VoiceEngines(tts=FakeTTS(), stt=stt, vad_factory=lambda: SileroVAD(_SILERO), wake=self.spotter)
        v = VoiceSession(msgs.append, lambda t: submitted.append(t) or True, lambda: None, lambda: False,
                         engines=eng, config=VoiceConfig(debug=False))
        v.start()
        v.set_listening(True)
        try:
            feed_in_chunks(v, pcm, 640)
            wait_for(lambda: v._handled >= 1, 10)
            time.sleep(0.2)
        finally:
            v.stop()
        return submitted, stt, msgs

    def test_hey_ember_with_command_is_heard_in_most_voices(self):
        wins = 0
        for voice, lang in self.VOICES:
            submitted, stt, _ = self.run_session(self.speak("Hey Ember, what time is it?", voice, lang), "what time is it")
            if submitted == ["what time is it"]:
                wins += 1
                self.assertLess(len(stt.audios[0]) / SR, 1.8)      # Whisper got the command part, not the whole utterance
        self.assertGreaterEqual(wins, 3, f"acoustic wake fired in only {wins}/4 voices")

    def test_bare_hey_ember_needs_no_transcription(self):
        wins = 0
        for voice, lang in self.VOICES:
            submitted, stt, msgs = self.run_session(self.speak("Hey Ember", voice, lang), "hey ember")
            if any(m.get("event") == "wake" for m in msgs if m["type"] == "voice_event") and stt.calls == 0:
                wins += 1
        self.assertGreaterEqual(wins, 3)

    def test_confusable_phrases_do_not_wake_her(self):
        for text in ("remember to buy milk", "what time is it", "Amber alert issued in three states",
                     "December is cold this year", "the members of the team arrived", "I need to embed this file"):
            for voice, lang in self.VOICES[:2]:
                submitted, _, msgs = self.run_session(self.speak(text, voice, lang), "what time is it")
                self.assertEqual(submitted, [], f"{text!r} in {voice} woke her")
                self.assertFalse([m for m in msgs if m.get("event") == "wake"], f"{text!r} in {voice}")


if __name__ == "__main__":
    unittest.main()
