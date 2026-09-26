"""
Tests for voice_tune.py: the recommendation logic, .env updating, level/onset helpers, and (when the
wake-word and Kokoro models are present) a small end-to-end evaluation on synthesized speech.

Run from the project root:  python -m unittest tests.test_voice_tune
"""
import os
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import voice_tune  # noqa: E402
from tests.test_voice_wake import HAVE_REAL, KWS_DIR, _KOKORO, _SILERO  # noqa: E402


def row(hey=(0, 4), ember=(0, 4), wake=(0, 2), strong=(0, 8), soft=(0, 8)):
    return {"hey": hey, "ember": ember, "wake": wake, "neg_strong": strong, "neg_soft": soft}


class RecommendTests(unittest.TestCase):
    def test_picks_most_sensitive_setting_with_no_false_wakes(self):
        results = {
            (1.0, 0.25): row(hey=(2, 4)),
            (2.0, 0.15): row(hey=(4, 4)),                       # best recall, still clean
            (3.0, 0.10): row(hey=(4, 4), strong=(3, 8)),        # same recall but false wakes -> rejected
        }
        rec = voice_tune.recommend(results)
        self.assertEqual(rec["hey"], (2.0, 0.15))
        self.assertTrue(rec["hey_clean"])

    def test_ties_go_to_the_less_sensitive_setting(self):
        results = {(1.0, 0.25): row(hey=(4, 4)), (2.0, 0.15): row(hey=(4, 4)), (3.0, 0.10): row(hey=(4, 4))}
        self.assertEqual(voice_tune.recommend(results)["hey"], (1.0, 0.25))

    def test_soft_setting_tolerates_one_false_wake_but_hey_tolerates_none(self):
        results = {
            (1.0, 0.25): row(hey=(3, 4), ember=(1, 4)),
            (2.0, 0.15): row(hey=(4, 4), ember=(4, 4), wake=(2, 2), strong=(1, 8), soft=(1, 8)),
        }
        rec = voice_tune.recommend(results)
        self.assertEqual(rec["hey"], (1.0, 0.25))            # a strong false wake disqualifies (2.0, 0.15) for "Hey Ember"
        self.assertEqual(rec["soft"], (2.0, 0.15))           # one soft false wake is tolerated
        self.assertTrue(rec["hey_clean"] and rec["soft_clean"])

    def test_when_nothing_is_clean_the_least_bad_setting_is_chosen_and_flagged(self):
        results = {(1.0, 0.25): row(hey=(4, 4), strong=(5, 8)), (2.0, 0.15): row(hey=(4, 4), strong=(2, 8))}
        rec = voice_tune.recommend(results)
        self.assertEqual(rec["hey"], (2.0, 0.15))
        self.assertFalse(rec["hey_clean"])

    def test_soft_false_wakes_do_not_count_against_the_hey_setting(self):
        results = {(1.0, 0.25): row(hey=(2, 4), soft=(6, 8)), (2.0, 0.15): row(hey=(4, 4), soft=(6, 8))}
        rec = voice_tune.recommend(results)
        self.assertEqual(rec["hey"], (2.0, 0.15))
        self.assertTrue(rec["hey_clean"])
        self.assertFalse(rec["soft_clean"])

    def test_env_lines(self):
        rec = {"hey": (2.0, 0.15), "soft": (1.5, 0.2)}
        self.assertEqual(voice_tune.env_lines(rec), {
            "EMBER_KWS_SCORE": "2.0", "EMBER_KWS_THRESHOLD": "0.15",
            "EMBER_KWS_SOFT_SCORE": "1.5", "EMBER_KWS_SOFT_THRESHOLD": "0.2"})


class EnvFileTests(unittest.TestCase):
    def test_upsert_replaces_appends_and_keeps_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w") as f:
                f.write("GEMINI_API_KEY=abc\n# note\nEMBER_KWS_THRESHOLD=0.25\nEMBER_TRANSPORT_TOKEN=tok")
            voice_tune.upsert_env(path, {"EMBER_KWS_THRESHOLD": "0.15", "EMBER_KWS_SCORE": "2.0"})
            with open(path) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[:2], ["GEMINI_API_KEY=abc", "# note"])
            self.assertIn("EMBER_KWS_THRESHOLD=0.15", lines)
            self.assertNotIn("EMBER_KWS_THRESHOLD=0.25", lines)
            self.assertIn("EMBER_TRANSPORT_TOKEN=tok", lines)
            self.assertIn("EMBER_KWS_SCORE=2.0", lines)
            self.assertEqual(sum(l.startswith("EMBER_KWS_THRESHOLD") for l in lines), 1)

    def test_creates_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            voice_tune.upsert_env(path, {"A": "1"})
            with open(path) as f:
                self.assertEqual(f.read(), "A=1\n")


class HelperTests(unittest.TestCase):
    def test_normalization_lifts_quiet_speech_within_limits(self):
        t = np.arange(16000)
        quiet = (0.02 * np.sin(t * 0.1)).astype(np.float32)
        out = voice_tune.normalize_like_client(quiet)
        self.assertGreater(float(np.abs(out).max()), 0.07)          # 0.02-peak speech lifted ~4x toward the target level
        self.assertLessEqual(float(np.abs(out).max()), 0.98)
        loud = (0.9 * np.sin(t * 0.1)).astype(np.float32)
        self.assertLessEqual(float(np.abs(voice_tune.normalize_like_client(loud)).max()), 0.98)
        silent = np.zeros(8000, np.float32)
        self.assertEqual(float(np.abs(voice_tune.normalize_like_client(silent)).max()), 0.0)

    def test_one_loud_pop_does_not_cap_the_boost_for_the_whole_clip(self):
        t = np.arange(32000)
        speech = (0.02 * np.sin(t * 0.1)).astype(np.float32)
        speech[100:180] += 0.9 * np.sin(np.arange(80) * 0.6).astype(np.float32)      # a pop
        out = voice_tune.normalize_like_client(speech)
        body = out[8000:]                                                           # away from the pop
        self.assertGreater(float(np.abs(body).max()), 0.06)                          # speech still lifted ~4x, not left at 0.02

    def test_wav_roundtrip_and_load_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = (0.4 * np.sin(np.arange(8000) * 0.05)).astype(np.float32)
            voice_tune.write_wav(os.path.join(tmp, "hey", "01.wav"), a)
            voice_tune.write_wav(os.path.join(tmp, "neg", "01.wav"), a)
            clips = voice_tune.load_dir(tmp)
            self.assertEqual({g: len(v) for g, v in clips.items()}, {"hey": 1, "ember": 0, "wake": 0, "neg": 1})
            self.assertAlmostEqual(float(np.abs(clips["hey"][0] - a).max()), 0.0, delta=1e-3)

    def test_wake_result_applies_the_same_rule_as_the_live_session(self):
        SR = 16000
        seg = [(SR, 4 * SR)]                                            # one utterance: 1.0 s .. 4.0 s
        early_soft = (int(1.4 * SR), "ember")                           # 0.4 s after it started
        late_soft = (int(3.5 * SR), "ember")                            # 2.5 s after -> not a first word
        strong_late = (int(3.5 * SR), "hey_ember")
        self.assertEqual(voice_tune.wake_result([early_soft], seg)[0], early_soft)
        hit, why = voice_tune.wake_result([late_soft], seg)
        self.assertIsNone(hit)
        self.assertIn("FIRST thing said", why)
        self.assertEqual(voice_tune.wake_result([strong_late], seg)[0], strong_late)      # strong: anywhere
        self.assertEqual(voice_tune.wake_result([early_soft, strong_late], seg)[0], strong_late)   # strong beats soft
        self.assertIn("nothing that sounded", voice_tune.wake_result([], seg)[1])
        self.assertIn("no utterance", voice_tune.wake_result([early_soft], [])[1])

    def test_clip_outcome_classifies_wakes(self):
        self.assertEqual(voice_tune.clip_outcome("ember", (100, "ember")), (True, False, False))
        self.assertEqual(voice_tune.clip_outcome("hey", None), (False, False, False))
        self.assertEqual(voice_tune.clip_outcome("neg", (100, "hey_ember")), (False, True, False))
        self.assertEqual(voice_tune.clip_outcome("neg", (100, "wake_up")), (False, False, True))
        self.assertEqual(voice_tune.clip_outcome("neg", None), (False, False, False))

    def test_look_alike_negatives_are_recognized_from_the_prompt_list(self):
        texts = [t for g, t in voice_tune.PROMPTS if g == "neg"]
        self.assertIn(texts.index("Remember to buy milk"), voice_tune.VETOABLE_NEG)
        self.assertIn(texts.index("December is cold this year"), voice_tune.VETOABLE_NEG)
        self.assertNotIn(texts.index("What time is it"), voice_tune.VETOABLE_NEG)


class FakeSTT:
    """Answers by clip length: 16000 samples -> a hey clip, 24000 -> ember, 32000 -> wake, 48000 -> negative."""

    def __init__(self, table):
        self.table, self.options, self.calls = table, {}, 0

    def set_options(self, **kw):
        self.options.update(kw)

    def transcribe(self, audio):
        self.calls += 1
        return self.table.get(len(audio), "")


def fake_prepared(counts=(4, 4, 2, 6)):
    lengths = {"hey": 16000, "ember": 24000, "wake": 32000, "neg": 48000}
    out = {}
    for (group, n) in zip(("hey", "ember", "wake", "neg"), counts):
        out[group] = [voice_tune.Prepared(np.zeros(lengths[group] * 2 + 2, dtype=np.uint8).tobytes()[: lengths[group] * 2],
                                          [(0, lengths[group])], -25.0, 0.5, 0.0) for _ in range(n)]
    return out


class SttEvaluationTests(unittest.TestCase):
    def run_eval(self, table, configs=None):
        stts = {}

        def make(cfg):
            stt = stts.setdefault(cfg["model"], FakeSTT(table))
            stt.set_options(hotwords=cfg["hotwords"], beam_size=cfg["beam"])
            return stt
        configs = configs or voice_tune.stt_grid(["base.en"])
        return voice_tune.evaluate_stt(fake_prepared(), configs, make_stt=make), stts

    def test_counts_wakes_and_false_wakes_with_the_servers_own_transcript_check(self):
        table = {16000: "Hey Ember, what time is it", 24000: "Humber which is the next race", 32000: "wake up",
                 48000: "remember to buy milk"}
        results, _ = self.run_eval(table)
        r = results[("base.en", "Ember", 1)]
        self.assertEqual(r["found"], {"hey": (4, 4), "ember": (4, 4), "wake": (2, 2)})   # 'Humber' is an alias now
        self.assertEqual(r["false"], (0, 6))
        self.assertEqual(r["text"][("ember", 1)], "Humber which is the next race")

    def test_misses_and_false_wakes_are_reported(self):
        table = {16000: "Hello there, what time is it", 24000: "which is the next race", 32000: "wake up", 48000: "Ember is a nice name"}
        results, _ = self.run_eval(table, [{"model": "base.en", "hotwords": "Ember", "beam": 1}])
        r = results[("base.en", "Ember", 1)]
        self.assertEqual(r["found"]["hey"], (0, 4))
        self.assertEqual(r["found"]["ember"], (0, 4))
        self.assertEqual(r["false"], (6, 6))

    def test_options_are_switched_without_reloading(self):
        results, stts = self.run_eval({16000: "hey ember"})
        self.assertEqual(len(stts), 1)                                    # one model object served all four settings
        self.assertEqual(len(results), 4)                                 # hotwords on/off x beam 1/5
        self.assertEqual(stts["base.en"].calls, 4 * 16)

    def test_recommendation_prefers_more_wakes_then_speed(self):
        def res(found, false, secs):
            return {"found": {"hey": (found, 8), "ember": (0, 8), "wake": (0, 4)}, "false": (false, 16), "avg_s": secs,
                    "text": {}, "woke": {}}
        results = {("base.en", "Ember", 1): res(5, 0, 0.7), ("small.en", "Ember", 1): res(7, 0, 2.1),
                   ("small.en", "", 5): res(7, 0, 2.9), ("base.en", "", 1): res(8, 5, 0.6)}
        self.assertEqual(voice_tune.recommend_stt(results), ("small.en", "Ember", 1))   # most wakes; ties -> faster; 5 false wakes excluded
        self.assertEqual(voice_tune.stt_env_lines(("small.en", "", 5)),
                         {"EMBER_STT_MODEL": "small.en", "EMBER_STT_HOTWORDS": "", "EMBER_STT_BEAM": "5"})

    def test_report_prints_and_shows_combined_coverage(self):
        import contextlib
        import io
        table = {16000: "Hello there", 24000: "Ember what time", 32000: "", 48000: "the light was on"}
        results, _ = self.run_eval(table)
        best = voice_tune.recommend_stt(results)
        acoustic = {("hey", 1): True, ("hey", 2): True, ("wake", 1): True}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            voice_tune.print_stt_report(results, best, acoustic)
        out = buf.getvalue()
        self.assertIn("either one wakes her", out)
        self.assertIn("hey 2/4", out)              # the two the sound detector caught
        self.assertIn("Hello there", out)          # what Whisper wrote for the ones it missed
        self.assertIn("EMBER_STT_MODEL=base.en", out)


class WhisperWrapperTests(unittest.TestCase):
    def wrapper(self, fail_hotwords=False, **kw):
        import ember_voice

        class Seg:
            def __init__(self, text):
                self.text, self.no_speech_prob, self.avg_logprob = text, 0.0, 0.0

        class Model:
            def __init__(self):
                self.calls = []

            def transcribe(self, audio, **kwargs):
                if fail_hotwords and "hotwords" in kwargs:
                    raise TypeError("unexpected keyword argument 'hotwords'")
                self.calls.append(kwargs)
                return iter([Seg("hello there")]), None

        stt = ember_voice.WhisperSTT(**kw)
        stt._model = Model()
        return stt

    def test_defaults_and_overrides(self):
        stt = self.wrapper()
        stt.transcribe(np.ones(1600, dtype=np.float32) * 0.1)
        self.assertEqual(stt._model.calls[-1]["hotwords"], "Ember")
        self.assertEqual(stt._model.calls[-1]["beam_size"], 1)
        stt.set_options(hotwords="", beam_size=5)
        stt.transcribe(np.ones(1600, dtype=np.float32) * 0.1)
        self.assertNotIn("hotwords", stt._model.calls[-1])                 # "" turns it off
        self.assertEqual(stt._model.calls[-1]["beam_size"], 5)

    def test_older_faster_whisper_without_hotwords_still_works(self):
        stt = self.wrapper(fail_hotwords=True)
        self.assertEqual(stt.transcribe(np.ones(1600, dtype=np.float32) * 0.1), "hello there")
        self.assertNotIn("hotwords", stt._model.calls[-1])


class PreparedTests(unittest.TestCase):
    def test_audio_stats_and_description(self):
        t = np.arange(32000)
        quiet = (0.05 * np.sin(t * 0.1)).astype(np.float32)
        level, peak, clipped = voice_tune.audio_stats(quiet)
        self.assertAlmostEqual(peak, 0.05, delta=0.005)
        self.assertLess(level, -25)
        self.assertEqual(clipped, 0.0)
        hot = np.clip(1.5 * np.sin(t * 0.1), -1, 1).astype(np.float32)
        self.assertGreater(voice_tune.audio_stats(hot)[2], 1.0)            # a hard-clipped recording is flagged
        p = voice_tune.Prepared(b"\x00\x00" * 16000, [(8000, 16000)], -30.0, 0.2, 0.0)
        self.assertIn("0.5-1.0s", p.describe())
        self.assertIn("-30 dBFS", p.describe())
        self.assertEqual(len(p.segment_audio()), 8000)
        self.assertEqual(len(voice_tune.Prepared(b"\x00\x00" * 4000, [], -30.0, 0.2, 0.0).segment_audio()), 4000)


@unittest.skipUnless(HAVE_REAL, "wake-word / Kokoro models not downloaded")
class EndToEndTuneTests(unittest.TestCase):
    def test_tuning_on_synthesized_speech_finds_a_clean_setting(self):
        import ember_voice
        tts = ember_voice.KokoroTTS(*_KOKORO)

        def clip(text, voice, lang):
            s, sr = tts._load().create(text, voice=voice, speed=1.0, lang=lang)
            return np.interp(np.linspace(0, len(s) - 1, int(len(s) * 16000 / sr)), np.arange(len(s)), s).astype(np.float32)

        clips = {"hey": [], "ember": [], "wake": [], "neg": []}
        for voice, lang in (("bm_daniel", "en-gb"), ("af_heart", "en-us")):
            clips["hey"].append(clip("Hey Ember, what time is it?", voice, lang))
            clips["ember"].append(clip("Ember, what's the weather like tomorrow?", voice, lang))
            clips["neg"].append(clip("what time is it", voice, lang))
            clips["neg"].append(clip("I need to buy some milk", voice, lang))
        results, prepared = voice_tune.evaluate_grid(clips, scores=(1.0, 2.0), thresholds=(0.25, 0.15), model_dir=KWS_DIR,
                                                     vad_model_path=_SILERO)
        self.assertEqual(len(results), 4)
        rec = voice_tune.recommend(results)
        self.assertTrue(rec["hey_clean"])
        found, total = results[rec["hey"]]["hey"]
        self.assertGreaterEqual(found, 1, results)                # heard "Hey Ember" in at least one of the two voices
        self.assertEqual(results[rec["hey"]]["neg_strong"][0], 0)
        self.assertIsInstance(voice_tune.diagnose(prepared, rec["hey"], model_dir=KWS_DIR), list)

    def test_regression_recordings_that_start_late_and_have_a_pop_and_noise(self):
        """The tuner once decided 'did she say Ember FIRST?' from loudness after the volume boost. On a real,
        noisy mic the boosted background noise counted as speech, so it believed speech began at t=0 — and every
        bare 'Ember' (spoken ~1 s after GO) looked too late: 0 found at every setting. It now uses the production
        speech detector to find where the utterance starts. Deterministic version: real VAD on a real-shaped
        recording, with the detection time controlled."""
        import ember_voice
        tts = ember_voice.KokoroTTS(*_KOKORO)
        s, sr = tts._load().create("Ember, what's the weather like tomorrow?", voice="bm_daniel", speed=1.0, lang="en-gb")
        speech = np.interp(np.linspace(0, len(s) - 1, int(len(s) * 16000 / sr)), np.arange(len(s)), s).astype(np.float32) * 0.15
        rng = np.random.default_rng(3)
        lead = rng.normal(0, 0.002, 16000).astype(np.float32)                    # 1 s of room noise before speaking
        lead[100:180] += 0.9 * np.sin(np.arange(80) * 0.6).astype(np.float32)   # the start-of-recording pop
        recording = np.concatenate([lead, speech + rng.normal(0, 0.002, len(speech)).astype(np.float32),
                                    rng.normal(0, 0.002, 16000).astype(np.float32)])

        # (1) this recording really does fool a loudness-based onset guess (the old method), i.e. it's the right scenario
        boosted = voice_tune.normalize_like_client(recording)
        n = len(boosted) // 512
        rms = np.sqrt(np.mean(boosted[: n * 512].reshape(n, 512) ** 2, axis=1))
        old_onset = float(np.where(rms > max(0.02, 0.25 * float(rms.max())))[0][0] * 512 / 16000)
        self.assertLess(old_onset, 0.7, "scenario no longer reproduces the old failure")     # speech actually starts at ~1.5 s

        # (2) the production speech detector places the utterance where the speech really is
        pcm, segments = voice_tune.prepare_clip(recording, _SILERO)
        self.assertEqual(len(segments), 1)
        start = segments[0][0]
        self.assertGreater(start / 16000, 1.2)                                                # 0.5 s lead-in + ~1 s before speaking

        # (3) so a bare "Ember" heard ~0.8 s into the utterance counts as first-word (the old logic rejected it)
        hit = (start + int(0.8 * 16000), "ember")
        self.assertEqual(voice_tune.wake_result([hit], segments)[0], hit)
        late = (start + int(2.5 * 16000), "ember")
        self.assertIsNone(voice_tune.wake_result([late], segments)[0])                        # genuinely mid-sentence: still rejected

if __name__ == "__main__":
    unittest.main()
