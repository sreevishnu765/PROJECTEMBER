"""
End-to-end: WebSocket transport -> VoiceSession -> the REAL ember_core.process_turn -> a fake model.
Checks that a spoken turn reaches the model with the spoken-reply prompt, a typed turn does not, the
reply streams back as chunks AND as audio, and the per-turn latency breakdown is produced.

Run from the project root:  python -m unittest tests.test_voice_core_integration
"""
import asyncio
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import ember_core  # noqa: E402
import ember_transport  # noqa: E402
import llm_client  # noqa: E402
from tests.test_transport_voice import TransportCase, TOKEN  # noqa: E402,F401


CAPTURED = []


def fake_stream(prompt, system_prompt="", use_search=False, history=None, cancel_check=None):
    CAPTURED.append(system_prompt)
    time.sleep(0.15)   # pretend first-token latency
    for piece in ("Half past six, ", "sir. Your evening ", "is clear."):
        yield llm_client.StreamChunk(text_delta=piece)
    yield llm_client.StreamChunk(done=True, source="cloud", model="fake-model")


class RealCoreOverTransport(TransportCase):
    async def asyncSetUp(self):
        # like TransportCase.asyncSetUp but with the real process_turn behind the server
        self._patch = mock.patch.object(llm_client, "generate_stream", fake_stream)
        self._patch.start()
        self._orig_fake_turn = ember_transport  # keep reference
        os.environ[ember_transport.AUTH_TOKEN_ENV] = TOKEN
        from tests.test_transport_voice import free_port
        import websockets
        self.port = free_port()
        self.engines = self.engines_factory()
        self.server = asyncio.create_task(
            ember_transport.run_transport_server(ember_core.process_turn, "localhost", self.port, voice_engines=self.engines))
        for _ in range(100):
            try:
                self.ws = await websockets.connect(f"ws://localhost:{self.port}")
                break
            except OSError:
                await asyncio.sleep(0.05)
        await self.ws.send(json.dumps({"type": "auth", "token": TOKEN}))
        self.assertEqual(json.loads(await self.ws.recv())["type"], "auth_ok")
        self.seen = []
        CAPTURED.clear()

    async def asyncTearDown(self):
        self._patch.stop()
        await super().asyncTearDown()

    async def test_spoken_turn_gets_spoken_prompt_typed_turn_does_not(self):
        with redirect_stdout(StringIO()):
            await self.send(type="message", text="tell me something interesting")
            await self.until(lambda m: m["type"] == "done")
            await self.send(type="voice", action="listen", on=True)
            await self.say("Ember, tell me something interesting")
            await self.until(lambda m: m["type"] == "done" and m["text"])
        self.assertEqual(len(CAPTURED), 2)
        self.assertNotIn("SPOKEN REPLY", CAPTURED[0])           # typed
        self.assertIn("SPOKEN REPLY", CAPTURED[1])              # spoken

    async def test_spoken_reply_streams_and_produces_audio_and_timing(self):
        with redirect_stdout(StringIO()):
            await self.send(type="voice", action="listen", on=True)
            await self.say("Ember, what is going on")
            await self.until(lambda m: m["type"] == "audio_chunk")
            timing = await self.until(lambda m: m["type"] == "voice_timing")
        chunks = [m for m in self.seen if m["type"] == "chunk"]
        self.assertGreaterEqual(len(chunks), 2)                 # arrived incrementally
        for key in ("endpoint_s", "stt_s", "first_text_s", "tts_s", "total_s"):
            self.assertIn(key, timing)
        self.assertGreaterEqual(timing["first_text_s"], 0.1)    # includes the fake model's latency
        self.assertIn("Half past six,", self.engines.tts.texts[0])   # first sentence spoken before the reply finished


if __name__ == "__main__":
    unittest.main()
