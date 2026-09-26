"""
Tests for the latency changes in llm_client.py: model order, Gemini thinking-level fallback ladder,
streaming through that ladder, the shared Tavily session, and warm-up. Uses a FAKE Gemini client
(no network, no API key needed, no quota touched).

Run from the project root:  python -m unittest tests.test_llm_latency
"""
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import llm_client  # noqa: E402
from google.genai import types  # noqa: E402


class FakeEvent:
    def __init__(self, text):
        self.text = text


class FakeModels:
    """behavior(model, config) -> iterable of FakeEvent, or a generator that raises when iterated."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []
        self.get_calls = []

    def generate_content_stream(self, model, contents, config=None):
        self.calls.append((model, config))
        return self.behavior(model, config)

    def generate_content(self, model, contents, config=None):
        self.calls.append((model, config))
        return self.behavior(model, config)

    def get(self, model):
        self.get_calls.append(model)


class FakeClient:
    def __init__(self, behavior):
        self.models = FakeModels(behavior)


def level_of(config):
    tc = getattr(config, "thinking_config", None) if config is not None else None
    return getattr(tc, "thinking_level", None) if tc is not None else None


def budget_of(config):
    tc = getattr(config, "thinking_config", None) if config is not None else None
    return getattr(tc, "thinking_budget", None) if tc is not None else None


def raising(exc):
    def gen():
        raise exc
        yield  # pragma: no cover
    return gen()


def stream_of(*texts):
    return iter([FakeEvent(t) for t in texts])


class LlmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        llm_client._thinking_choice.clear()
        # never touch the real quota_state.json
        self._quota = mock.patch.object(
            llm_client, "_quota", llm_client.QuotaTracker(os.path.join(self.tmp.name, "q.json"), llm_client.CLOUD_TIERS))
        self._quota.start()
        self._out = mock.patch("builtins.print")
        self.printed = self._out.start()

    def tearDown(self):
        self._out.stop()
        self._quota.stop()
        self.tmp.cleanup()
        llm_client._thinking_choice.clear()

    def use(self, behavior):
        client = FakeClient(behavior)
        p = mock.patch.object(llm_client, "_gemini_client", client)
        p.start()
        self.addCleanup(p.stop)
        return client

    def stream(self, **kw):
        return list(llm_client.generate_stream("hello", system_prompt="sys", history=[], **kw))

    def text(self, chunks):
        return "".join(c.text_delta for c in chunks)


class TierOrderTests(unittest.TestCase):
    def test_fast_model_first_by_default_quality_first_on_request(self):
        try:
            with mock.patch.dict(os.environ, {"EMBER_CLOUD_ORDER": "fast"}):
                importlib.reload(llm_client)
                self.assertEqual([t["model"] for t in llm_client.CLOUD_TIERS], ["gemini-3.5-flash-lite", "gemini-3.5-flash"])
            with mock.patch.dict(os.environ, {"EMBER_CLOUD_ORDER": "quality"}):
                importlib.reload(llm_client)
                self.assertEqual([t["model"] for t in llm_client.CLOUD_TIERS], ["gemini-3.5-flash", "gemini-3.5-flash-lite"])
        finally:
            with mock.patch.dict(os.environ, {"EMBER_CLOUD_ORDER": "fast"}):
                importlib.reload(llm_client)


class ThinkingLadderTests(LlmCase):
    def test_streams_with_minimal_thinking_first(self):
        client = self.use(lambda m, c: stream_of("Good ", "morning, ", "sir."))
        out = self.stream()
        self.assertEqual(self.text(out), "Good morning, sir.")
        self.assertTrue(out[-1].done)
        model, config = client.models.calls[0]
        self.assertEqual(model, "gemini-3.5-flash-lite")            # the fast model goes first
        self.assertEqual(level_of(config), types.ThinkingLevel.MINIMAL)
        self.assertEqual(len(client.models.calls), 1)

    def test_rejection_walks_down_the_ladder_then_remembers(self):
        def behavior(model, config):
            if level_of(config) == types.ThinkingLevel.MINIMAL:
                return raising(Exception("400 INVALID_ARGUMENT: thinking level minimal is not supported for this model"))
            return stream_of("Fine, sir.")
        client = self.use(behavior)
        self.assertEqual(self.text(self.stream()), "Fine, sir.")
        levels = [level_of(c) for _, c in client.models.calls]
        self.assertEqual(levels, [types.ThinkingLevel.MINIMAL, types.ThinkingLevel.LOW])
        # second call goes straight to the level that worked — no repeated failed attempts
        client.models.calls.clear()
        self.assertEqual(self.text(self.stream()), "Fine, sir.")
        self.assertEqual([level_of(c) for _, c in client.models.calls], [types.ThinkingLevel.LOW])

    def test_budget_zero_then_no_thinking_config_as_last_resorts(self):
        def only_budget(model, config):
            if level_of(config) is not None:
                return raising(Exception("400 INVALID_ARGUMENT: unsupported thinking_level"))
            return stream_of("ok")
        client = self.use(only_budget)
        self.stream()
        self.assertEqual(budget_of(client.models.calls[-1][1]), 0)

        llm_client._thinking_choice.clear()

        def nothing_supported(model, config):
            if config is not None and getattr(config, "thinking_config", None) is not None:
                return raising(Exception("400 INVALID_ARGUMENT: thinking is not supported by this model"))
            return stream_of("ok")
        client = self.use(nothing_supported)
        self.assertEqual(self.text(self.stream()), "ok")
        self.assertIsNone(client.models.calls[-1][1])                   # finally: no config at all

    def test_other_errors_are_not_mistaken_for_thinking_rejections(self):
        def flaky(model, config):
            if model == "gemini-3.5-flash-lite":
                return raising(Exception("429 RESOURCE_EXHAUSTED: quota"))
            return stream_of("from the second tier")
        client = self.use(flaky)
        out = self.stream()
        self.assertEqual(self.text(out), "from the second tier")
        models = [m for m, _ in client.models.calls]
        self.assertEqual(models, ["gemini-3.5-flash-lite", "gemini-3.5-flash"])   # exactly one attempt per tier
        self.assertEqual(out[-1].model, "gemini-3.5-flash")
        self.assertNotIn("gemini-3.5-flash-lite", llm_client._thinking_choice)     # a 429 teaches us nothing

    def test_empty_stream_falls_through_to_next_tier(self):
        client = self.use(lambda m, c: stream_of() if m.endswith("lite") else stream_of("answer"))
        self.assertEqual(self.text(self.stream()), "answer")

    def test_default_setting_sends_no_thinking_config(self):
        with mock.patch.object(llm_client, "GEMINI_THINKING", "default"):
            client = self.use(lambda m, c: stream_of("hi"))
            self.stream()
        self.assertIsNone(client.models.calls[0][1])

    def test_explicit_level_is_used_alone(self):
        with mock.patch.object(llm_client, "GEMINI_THINKING", "high"):
            client = self.use(lambda m, c: stream_of("hi"))
            self.stream()
        self.assertEqual(level_of(client.models.calls[0][1]), types.ThinkingLevel.HIGH)


class NonStreamingTests(LlmCase):
    def response(self, text):
        return mock.Mock(text=text, candidates=[mock.Mock(grounding_metadata=None)])

    def test_search_grounding_keeps_its_tool_and_gains_thinking(self):
        client = self.use(lambda m, c: self.response("grounded answer"))
        result = llm_client.generate("who won", system_prompt="s", use_search=True, history=[])
        self.assertEqual(result.text, "grounded answer")
        _, config = client.models.calls[0]
        self.assertTrue(config.tools and config.tools[0].google_search is not None)
        self.assertTrue(config.automatic_function_calling.disable)
        self.assertEqual(level_of(config), types.ThinkingLevel.MINIMAL)

    def test_plain_call_and_ladder(self):
        def behavior(model, config):
            if level_of(config) == types.ThinkingLevel.MINIMAL:
                raise Exception("400 INVALID_ARGUMENT thinking_level is not supported")
            return self.response("ok")
        client = self.use(behavior)
        self.assertEqual(llm_client.generate("hi", history=[]).text, "ok")
        self.assertEqual([level_of(c) for _, c in client.models.calls], [types.ThinkingLevel.MINIMAL, types.ThinkingLevel.LOW])


class TavilyAndWarmupTests(LlmCase):
    def test_search_reuses_one_session_with_the_short_timeout(self):
        fake = mock.Mock()
        fake.ok = True
        fake.json.return_value = {"results": [{"title": "t", "url": "https://x.example/a", "content": "c"}]}
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "k"}), \
                mock.patch.object(llm_client._tavily_session, "post", return_value=fake) as post:
            out = llm_client.web_search("porsche 963")
        self.assertEqual(out, [{"title": "t", "url": "https://x.example/a", "content": "c"}])
        self.assertEqual(post.call_args.kwargs["timeout"], llm_client.TAVILY_TIMEOUT_SECONDS)
        self.assertLessEqual(llm_client.TAVILY_TIMEOUT_SECONDS, 8.0)

    def test_search_failure_still_returns_none(self):
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "k"}), \
                mock.patch.object(llm_client._tavily_session, "post", side_effect=Exception("timeout")):
            self.assertIsNone(llm_client.web_search("anything"))

    def test_warm_up_costs_no_generation_quota(self):
        client = self.use(lambda m, c: stream_of("x"))
        with mock.patch.object(llm_client, "_get_local_embed_model", return_value=None) as embed:
            llm_client.warm_up()
        embed.assert_called_once()
        self.assertEqual(client.models.get_calls, ["gemini-3.5-flash-lite"])
        self.assertEqual(client.models.calls, [])

    def test_warm_up_survives_failures(self):
        client = self.use(lambda m, c: stream_of("x"))
        client.models.get = mock.Mock(side_effect=Exception("offline"))
        with mock.patch.object(llm_client, "_get_local_embed_model", side_effect=Exception("no model")):
            llm_client.warm_up()   # must not raise


if __name__ == "__main__":
    unittest.main()
