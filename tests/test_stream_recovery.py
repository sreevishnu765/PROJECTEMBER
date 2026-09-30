"""
Mid-stream auto-continue (llm_client.generate_stream) and clean history (ember_core.process_turn).
Fake provider streams only: no network, no API key, no quota touched.

Run from the project root:  python -m unittest tests.test_stream_recovery
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import llm_client  # noqa: E402

M1 = llm_client.CLOUD_TIERS[0]["model"]
M2 = llm_client.CLOUD_TIERS[1]["model"]


class Boom(Exception):
    pass


def script(*parts):
    """A fake provider stream: strings are yielded as deltas, an Exception instance is raised."""
    def gen():
        for p in parts:
            if isinstance(p, Exception):
                raise p
            yield p
    return gen()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []       # (model, prompt, system_prompt)
        self.behaviour = {}   # model -> list of parts
        quota = mock.MagicMock()
        quota.can_use.return_value = True
        self.patches = [
            mock.patch.object(llm_client, "cloud_available", lambda: True),
            mock.patch.object(llm_client, "_quota", quota),
            mock.patch.object(llm_client, "_tier_has_key", lambda t: False),
            mock.patch.object(llm_client, "_stream_gemini", self._fake_gemini),
            mock.patch.object(llm_client, "_stream_local", self._fake_local),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _fake_gemini(self, model, prompt, system_prompt, history):
        self.calls.append((model, prompt, system_prompt))
        return script(*self.behaviour[model])

    def _fake_local(self, prompt, system_prompt, history):
        self.calls.append((llm_client.OLLAMA_MODEL, prompt, system_prompt))
        return script(*self.behaviour[llm_client.OLLAMA_MODEL])

    def run_stream(self, prompt="explain drivetrains", **kw):
        chunks = list(llm_client.generate_stream(prompt, system_prompt="SYS", **kw))
        text = "".join(c.text_delta for c in chunks)
        return text, chunks[-1], chunks

    def test_healthy_stream_is_untouched(self):
        self.behaviour[M1] = ["Hello ", "world."]
        text, final, _ = self.run_stream()
        self.assertEqual(text, "Hello world.")
        self.assertIsNone(final.error)
        self.assertIsNone(final.recovered_from)
        self.assertEqual(len(self.calls), 1)

    def test_mid_stream_failure_continues_on_next_tier_and_stitches_cleanly(self):
        self.behaviour[M1] = ["Front-wheel drive sends power to the front wheels. ", "Rear-wheel drive sends", Boom("504 DEADLINE_EXCEEDED")]
        self.behaviour[M2] = [" power to the rear wheels, ", "which improves balance."]
        text, final, _ = self.run_stream()
        self.assertEqual(
            text,
            "Front-wheel drive sends power to the front wheels. Rear-wheel drive sends"
            " power to the rear wheels, which improves balance.",
        )
        self.assertIsNone(final.error)
        self.assertIn("504", final.recovered_from)
        self.assertEqual(final.model, M2)
        # the continuation request carried the original prompt AND the partial answer
        model, prompt, system = self.calls[1]
        self.assertEqual(model, M2)
        self.assertIn("explain drivetrains", prompt)
        self.assertIn("Rear-wheel drive sends", prompt)
        self.assertIn("SYS", system)
        self.assertIn("cut off", system)

    def test_repeated_tail_is_trimmed(self):
        self.behaviour[M1] = ["AWD sends power to all four wheels via a center differential", Boom("504")]
        self.behaviour[M2] = ["a center differential, which improves grip."]
        text, _, _ = self.run_stream()
        self.assertEqual(text, "AWD sends power to all four wheels via a center differential, which improves grip.")

    def test_word_boundary_restored_when_model_strips_leading_space(self):
        self.behaviour[M1] = ["Used in dedicated off-roaders and traditional large", Boom("504")]
        self.behaviour[M2] = ["SUVs."]
        text, _, _ = self.run_stream()
        self.assertEqual(text, "Used in dedicated off-roaders and traditional large SUVs.")

    def test_failed_tier_is_not_retried_and_falls_through_to_local(self):
        self.behaviour[M1] = ["Part one, ", Boom("504")]
        self.behaviour[M2] = ["part two, ", Boom("503")]
        self.behaviour[llm_client.OLLAMA_MODEL] = ["and part three."]
        text, final, _ = self.run_stream()
        self.assertEqual(text, "Part one, part two, and part three.")
        self.assertEqual([c[0] for c in self.calls], [M1, M2, llm_client.OLLAMA_MODEL])  # each tier at most once
        self.assertIsNone(final.error)

    def test_every_tier_failing_returns_partial_plus_original_error(self):
        self.behaviour[M1] = ["Part one, ", Boom("504 first")]
        self.behaviour[M2] = ["part two, ", Boom("503 second")]
        self.behaviour[llm_client.OLLAMA_MODEL] = [Boom("local down")]
        text, final, _ = self.run_stream()
        self.assertTrue(text.startswith("Part one,"))
        self.assertIsNotNone(final.error)  # honest failure, nothing hidden

    def test_recovery_hops_are_bounded(self):
        self.behaviour[M1] = ["a ", Boom("504")]
        self.behaviour[M2] = ["b ", Boom("504")]
        self.behaviour[llm_client.OLLAMA_MODEL] = ["c ", Boom("504")]
        _, final, _ = self.run_stream()
        self.assertIsNotNone(final.error)
        self.assertLessEqual(len(self.calls), 1 + llm_client.MAX_CONTINUATIONS)

    def test_failure_before_any_text_still_just_tries_next_tier(self):
        self.behaviour[M1] = [Boom("429 quota")]
        self.behaviour[M2] = ["Fine."]
        text, final, _ = self.run_stream()
        self.assertEqual(text, "Fine.")
        self.assertIsNone(final.recovered_from)   # this is ordinary fallback, not recovery
        self.assertNotIn("cut off", self.calls[1][2])

    def test_cancel_during_continuation_stops_it(self):
        self.behaviour[M1] = ["Start, ", Boom("504")]
        self.behaviour[M2] = ["more ", "and more ", "and more."]
        state = {"n": 0}

        def cancel():
            state["n"] += 1
            return state["n"] > 3   # lets M1's first delta through, then cancels the continuation

        _, final, _ = self.run_stream(cancel_check=cancel)
        self.assertTrue(final.cancelled)


class HistoryCleanTests(unittest.TestCase):
    """process_turn must never write the failure note into conversation history."""

    def _run(self, chunks):
        import ember_core
        from ember_conversation import EmberConversation

        def fake_stream(prompt, system_prompt="", use_search=False, history=None, cancel_check=None):
            yield from chunks

        conv = EmberConversation("hist-test")
        with mock.patch.object(llm_client, "generate_stream", fake_stream):
            tag, reply = ember_core.process_turn("explain drivetrains", conv, stream_callback=lambda *a: None)
        return tag, reply, conv.snapshot_history()

    def test_unrecoverable_cut_off_shows_note_to_user_but_not_to_history(self):
        tag, reply, hist = self._run([
            llm_client.StreamChunk(text_delta="FWD, RWD, AWD and"),
            llm_client.StreamChunk(done=True, source="cloud", model="fake",
                                   error="Stream interrupted: 504 DEADLINE_EXCEEDED"),
        ])
        self.assertIn("cut off", reply)                   # the person is told
        self.assertNotIn("504", reply)                    # ...without the raw provider blob
        assistant = hist[-1]["content"]
        self.assertEqual(assistant, "FWD, RWD, AWD and")  # history holds only what the model wrote
        self.assertNotIn("cut off", assistant)
        self.assertNotIn("504", assistant)

    def test_recovered_reply_is_clean_everywhere(self):
        tag, reply, hist = self._run([
            llm_client.StreamChunk(text_delta="Full answer."),
            llm_client.StreamChunk(done=True, source="cloud", model="fake", recovered_from="504"),
        ])
        self.assertEqual(reply, "Full answer.")
        self.assertEqual(hist[-1]["content"], "Full answer.")


if __name__ == "__main__":
    unittest.main()
