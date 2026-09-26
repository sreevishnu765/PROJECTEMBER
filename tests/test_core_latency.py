"""
Tests for the ember_core.py latency changes: the spoken-reply system-prompt addendum, timing lines,
and the startup warm-up hook. Runs the REAL process_turn() with a fake model behind it (no network).

Run from the project root:  python -m unittest tests.test_core_latency
"""
import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import ember_core  # noqa: E402
import ember_research  # noqa: E402
import llm_client  # noqa: E402
from ember_conversation import EmberConversation  # noqa: E402


def fake_stream_factory(captured, pieces=("Certainly, sir. ", "All done.")):
    def _fake(prompt, system_prompt="", use_search=False, history=None, cancel_check=None):
        captured.append({"prompt": prompt, "system": system_prompt, "use_search": use_search})
        for p in pieces:
            yield llm_client.StreamChunk(text_delta=p)
        yield llm_client.StreamChunk(done=True, source="cloud", model="fake-model")
    return _fake


class CoreCase(unittest.TestCase):
    def turn(self, text, spoken=None, **kw):
        conv = EmberConversation("t-" + str(id(self)))
        if spoken is not None:
            conv.spoken_reply = spoken
        seen, deltas = [], []
        out = io.StringIO()
        with mock.patch.object(llm_client, "generate_stream", fake_stream_factory(seen, **kw)), redirect_stdout(out):
            tag, reply = ember_core.process_turn(text, conv, stream_callback=lambda d, kind="text": deltas.append((kind, d)))
        return seen, tag, reply, out.getvalue(), deltas


class SpokenModeTests(CoreCase):
    def test_typed_turn_gets_the_normal_prompt(self):
        seen, tag, reply, _, _ = self.turn("tell me something interesting", spoken=False)
        self.assertEqual(reply, "Certainly, sir. All done.")
        self.assertNotIn("SPOKEN REPLY", seen[0]["system"])
        self.assertIn("You are Ember", seen[0]["system"])

    def test_turn_with_no_flag_at_all_is_unchanged(self):
        seen, *_ = self.turn("tell me something interesting")
        self.assertNotIn("SPOKEN REPLY", seen[0]["system"])

    def test_spoken_turn_asks_for_short_plain_speech(self):
        seen, *_ = self.turn("tell me something interesting", spoken=True)
        system = seen[0]["system"]
        self.assertIn(ember_core.SPOKEN_ADDENDUM, system)
        self.assertIn("no markdown", system)
        self.assertLess(system.index("You are Ember"), system.index("SPOKEN REPLY"))   # addendum comes after the persona

    def test_spoken_turn_with_search_evidence_still_gets_the_addendum(self):
        evidence = [{"title": "Jet engine", "url": "https://a.example/x", "content": "A jet engine is a gas turbine that makes thrust."},
                    {"title": "Turbofan", "url": "https://b.example/y", "content": "Jet engine thrust comes from a high speed exhaust."}]
        result = ember_research.ResearchResult(evidence=evidence, rounds_used=1, queries_tried=["q"], relevant=True)
        with mock.patch.object(ember_research, "research", return_value=result):
            seen, tag, reply, printed, deltas = self.turn("what can you tell me about jet engines", spoken=True)
        self.assertIn("SPOKEN REPLY", seen[0]["system"])
        self.assertIn("Live search evidence", seen[0]["prompt"])
        self.assertIn("[ember_core] research: 2 source(s), 1 round(s)", printed)
        self.assertTrue(any(kind == "status" for kind, _ in deltas))          # "Searching for that, sir..." still emitted


class TimingTests(CoreCase):
    def test_first_token_and_total_lines_are_printed(self):
        _, _, _, printed, _ = self.turn("tell me something interesting")
        self.assertIn("[ember_core] first token", printed)
        self.assertIn("[ember_core] turn total", printed)
        self.assertIn("streamed", printed)

    def test_local_intents_are_unaffected(self):
        conv = EmberConversation("t-local")
        conv.spoken_reply = True
        with mock.patch.object(llm_client, "generate_stream", side_effect=AssertionError("must not call the model")):
            tag, reply = ember_core.process_turn("what time is it", conv, stream_callback=lambda *a: None)
        self.assertEqual(tag, "get_time")
        self.assertIn("sir", reply)


class WarmUpTests(unittest.TestCase):
    def test_warm_up_models_returns_immediately_and_runs_llm_warm_up_in_a_thread(self):
        started, release, ran_in = threading.Event(), threading.Event(), []

        def slow_warm_up():
            ran_in.append(threading.current_thread().name)
            started.set()
            release.wait(5)

        with mock.patch.object(llm_client, "warm_up", slow_warm_up):
            t0 = __import__("time").perf_counter()
            ember_core.warm_up_models()
            elapsed = __import__("time").perf_counter() - t0
            self.assertTrue(started.wait(2))
            release.set()
        self.assertLess(elapsed, 1.0)                                # didn't block on the slow warm-up
        self.assertEqual(ran_in, ["ember-warmup"])


if __name__ == "__main__":
    unittest.main()
