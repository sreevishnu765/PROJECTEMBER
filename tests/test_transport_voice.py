"""
End-to-end tests for ember_transport.py's voice wiring: a REAL WebSocket
server, a fake process_turn (no LLM), and fake STT/TTS/VAD engines (no
models) — so this runs anywhere, in a couple of seconds, and checks the
plumbing (binary mic frames in, transcript/chunk/audio out, cancel,
barge-in, graceful "voice unavailable") rather than model quality.

Run from the project root:  python -m unittest tests.test_transport_voice
"""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
import wave

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import websockets
    import ember_query_registry  # noqa: F401  (the real one, in the real project)
except ImportError as e:
    if "ember_query_registry" in str(e):
        stub = types.ModuleType("ember_query_registry")

        class _StubRegistry:
            def dispatch(self, name, params, confirm_gate, conversation):
                return {"ok": True, "data": {"pong": name}}

        stub.get_query_registry = lambda: _StubRegistry()
        sys.modules["ember_query_registry"] = stub
        import websockets  # noqa: F811
    else:
        raise

import ember_transport  # noqa: E402
from ember_voice import VoiceConfig, VoiceEngines, VoiceModelsMissing  # noqa: E402
from tests.test_voice_session import FakeSTT, FakeTTS, FakeVAD, silence_bytes, speech_bytes  # noqa: E402

TOKEN = "test-token"


def free_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


SPOKEN_FLAGS = []


def fake_turn(text, conversation, stream_callback=None):
    """Stands in for ember_core.process_turn. 'slow ...' streams until cancelled."""
    conversation.reset_cancel()
    SPOKEN_FLAGS.append((text, getattr(conversation, "spoken_reply", None)))
    if text.startswith("slow"):
        for i in range(200):
            if conversation.is_cancelled():
                return "cancelled", "Cancelled, sir."
            stream_callback(f"Part number {i} of a long story, sir. ", "text")
            time.sleep(0.05)
        return "fake", "long story"
    reply = f"You said {text}, sir. All good."
    if stream_callback:
        for word in reply.split(" "):
            stream_callback(word + " ")
    return "fake", reply


class BrokenEngines:
    tts = None
    stt = None

    def new_vad(self):
        raise VoiceModelsMissing("no models here")


class TransportCase(unittest.IsolatedAsyncioTestCase):
    engines_factory = staticmethod(lambda: VoiceEngines(tts=FakeTTS(), stt=FakeSTT(), vad_factory=FakeVAD))

    async def asyncSetUp(self):
        os.environ[ember_transport.AUTH_TOKEN_ENV] = TOKEN
        self.port = free_port()
        self.engines = self.engines_factory()
        self.server = asyncio.create_task(
            ember_transport.run_transport_server(fake_turn, "localhost", self.port, voice_engines=self.engines)
        )
        for _ in range(100):
            try:
                self.ws = await websockets.connect(f"ws://localhost:{self.port}")
                break
            except OSError:
                await asyncio.sleep(0.05)
        await self.ws.send(json.dumps({"type": "auth", "token": TOKEN}))
        self.assertEqual(json.loads(await self.ws.recv())["type"], "auth_ok")
        self.seen = []

    async def asyncTearDown(self):
        await self.ws.close()
        self.server.cancel()
        try:
            await self.server
        except (asyncio.CancelledError, Exception):
            pass

    async def send(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def until(self, pred, timeout=5.0):
        """Reads messages (recording all of them in self.seen) until pred(msg) is true."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=max(0.05, end - time.time()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            self.seen.append(msg)
            if pred(msg):
                return msg
        self.fail(f"timed out; saw types: {[m['type'] for m in self.seen][-15:]}")

    def types(self):
        return [m["type"] for m in self.seen]

    async def say(self, text):
        self.engines.stt.script.append(text)
        await self.ws.send(speech_bytes())
        await self.ws.send(silence_bytes())


class TransportVoiceTests(TransportCase):
    async def test_typed_chat_unchanged_and_silent(self):
        await self.send(type="message", text="hello")
        done = await self.until(lambda m: m["type"] == "done")
        self.assertEqual(done["text"], "You said hello, sir. All good.")
        self.assertIn("chunk", self.types())
        self.assertNotIn("audio_chunk", self.types())

    async def test_spoken_flag_tells_the_turn_whether_it_will_be_read_aloud(self):
        SPOKEN_FLAGS.clear()
        await self.send(type="message", text="typed one")
        await self.until(lambda m: m["type"] == "done")
        await self.send(type="voice", action="listen", on=True)
        await self.say("Ember, spoken one")
        await self.until(lambda m: m["type"] == "done" and "spoken one" in m["text"])
        await self.send(type="voice", action="speaker", on=True)
        await self.send(type="message", text="typed with speaker")
        await self.until(lambda m: m["type"] == "done" and "typed with speaker" in m["text"])
        self.assertEqual(SPOKEN_FLAGS, [("typed one", False), ("spoken one", True), ("typed with speaker", True)])

    async def test_query_still_works(self):
        await self.send(type="query", id="q1", name="ping", params={})
        res = await self.until(lambda m: m["type"] == "query_result")
        self.assertTrue(res["ok"])
        self.assertEqual(res["id"], "q1")

    async def test_voice_roundtrip(self):
        await self.send(type="voice", action="listen", on=True)
        await self.until(lambda m: m["type"] == "voice_state" and m["listening"])
        await self.say("Hey Ember, what's the date")

        tr = await self.until(lambda m: m["type"] == "transcript")
        self.assertEqual(tr["text"], "what's the date")
        self.assertEqual(tr["trigger"], "wake")
        done = await self.until(lambda m: m["type"] == "done")
        self.assertIn("You said what's the date", done["text"])
        audio = await self.until(lambda m: m["type"] == "audio_chunk")
        self.assertEqual(audio["sample_rate"], 24000)
        self.assertGreater(len(base64.b64decode(audio["data"])), 1000)
        self.assertEqual(self.engines.tts.texts[0], "You said what's the date, sir.")

    async def test_follow_up_without_wake_word(self):
        await self.send(type="voice", action="listen", on=True)
        await self.say("Ember, first question")
        await self.until(lambda m: m["type"] == "done")
        await self.until(lambda m: m["type"] == "voice_state" and m["awake"], timeout=6)
        await asyncio.sleep(0.6)   # half-duplex: the mic re-opens a moment after she stops
        await self.say("and a second one")
        tr = await self.until(lambda m: m["type"] == "transcript" and m["trigger"] == "follow_up")
        self.assertEqual(tr["text"], "and a second one")

    async def test_voice_mode_toggle_from_hud(self):
        await self.send(type="voice", action="listen", on=True)
        await self.send(type="voice", action="mode", on=True)
        await self.until(lambda m: m["type"] == "voice_state" and m["voice_mode"])
        await self.say("no wake word here")
        tr = await self.until(lambda m: m["type"] == "transcript")
        self.assertEqual(tr["trigger"], "voice_mode")

    async def test_ambient_speech_never_reaches_a_turn(self):
        await self.send(type="voice", action="listen", on=True)
        await self.say("just people chatting nearby")
        await asyncio.sleep(0.5)
        try:
            while True:
                self.seen.append(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=0.2)))
        except asyncio.TimeoutError:
            pass
        self.assertNotIn("transcript", self.types())
        self.assertNotIn("chunk", self.types())

    async def test_cancel_button_silences_speech(self):
        await self.send(type="voice", action="speaker", on=True)
        await self.send(type="message", text="slow story please")
        await self.until(lambda m: m["type"] == "audio_chunk")
        await self.send(type="cancel")
        stop = await self.until(lambda m: m["type"] == "audio_stop")
        self.assertIn("gen", stop)
        await self.until(lambda m: m["type"] == "done")
        n_before = self.types().count("audio_chunk")
        await asyncio.sleep(0.5)
        try:
            while True:
                self.seen.append(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=0.2)))
        except asyncio.TimeoutError:
            pass
        # nothing from the cancelled turn is spoken after the stop
        late = [m for m in self.seen[self.seen.index(stop):] if m["type"] == "audio_chunk" and m["gen"] < stop["gen"]]
        self.assertEqual(late, [])
        self.assertGreaterEqual(n_before, 1)

    async def test_barge_in_by_voice(self):
        await self.send(type="voice", action="listen", on=True)
        await self.send(type="voice", action="barge_in", on=True)   # default is half-duplex
        await self.say("Ember, slow story please")
        await self.until(lambda m: m["type"] == "audio_chunk")
        await self.say("Ember, what time is it")
        await self.until(lambda m: m["type"] == "audio_stop")
        tr = await self.until(lambda m: m["type"] == "transcript" and m["text"] == "what time is it")
        self.assertEqual(tr["trigger"], "wake")
        done = await self.until(lambda m: m["type"] == "done" and "You said what time is it" in m["text"])
        self.assertEqual(done["tag"], "fake")

    async def test_stop_command_is_local(self):
        await self.send(type="voice", action="listen", on=True)
        await self.send(type="voice", action="barge_in", on=True)   # default is half-duplex
        await self.say("Ember, slow story please")
        await self.until(lambda m: m["type"] == "audio_chunk")
        await self.say("Ember, stop")
        await self.until(lambda m: m["type"] == "voice_event" and m["event"] == "stopped")
        done = await self.until(lambda m: m["type"] == "done")
        self.assertEqual(done["tag"], "cancelled")
        self.assertEqual([m for m in self.seen if m["type"] == "transcript" and m["text"] == "stop"], [])

    async def test_typed_message_while_turn_running_still_rejected(self):
        await self.send(type="message", text="slow one")
        await asyncio.sleep(0.2)
        await self.send(type="message", text="second")
        err = await self.until(lambda m: m["type"] == "error")
        self.assertIn("already in progress", err["message"])
        await self.send(type="cancel")


class TransportVoiceUnavailableTests(TransportCase):
    engines_factory = staticmethod(lambda: BrokenEngines())

    async def test_voice_unavailable_but_chat_fine(self):
        await self.send(type="voice", action="listen", on=True)
        msg = await self.until(lambda m: m["type"] == "voice_unavailable")
        self.assertIn("no models here", msg["reason"])
        await self.ws.send(speech_bytes())   # binary frames are ignored, not fatal
        await self.send(type="message", text="still works")
        done = await self.until(lambda m: m["type"] == "done")
        self.assertIn("still works", done["text"])


class TokenProvisioningTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(ember_transport.AUTH_TOKEN_ENV, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = os.path.join(self.tmp.name, ".env")

    def tearDown(self):
        os.environ.pop(ember_transport.AUTH_TOKEN_ENV, None)
        if self._saved is not None:
            os.environ[ember_transport.AUTH_TOKEN_ENV] = self._saved
        self.tmp.cleanup()

    def test_environment_wins_and_the_file_is_untouched(self):
        os.environ[ember_transport.AUTH_TOKEN_ENV] = "from-env"
        self.assertEqual(ember_transport.ensure_token(self.env_path), "from-env")
        self.assertFalse(os.path.exists(self.env_path))

    def test_uses_a_token_already_in_dotenv(self):
        with open(self.env_path, "w") as f:
            f.write("# comment\nGEMINI_API_KEY=abc\nEMBER_TRANSPORT_TOKEN='quoted-token'\n")
        self.assertEqual(ember_transport.ensure_token(self.env_path), "quoted-token")
        self.assertEqual(os.environ[ember_transport.AUTH_TOKEN_ENV], "quoted-token")

    def test_generates_saves_and_reuses_a_token_without_touching_other_lines(self):
        with open(self.env_path, "w") as f:
            f.write("GEMINI_API_KEY=abc")                     # note: no trailing newline
        token = ember_transport.ensure_token(self.env_path)
        self.assertGreaterEqual(len(token), 24)
        text = open(self.env_path).read()
        self.assertIn("GEMINI_API_KEY=abc\n", text)          # existing line intact, newline added
        self.assertIn(f"EMBER_TRANSPORT_TOKEN={token}", text)
        os.environ.pop(ember_transport.AUTH_TOKEN_ENV)         # a fresh process later: same token comes back from the file
        self.assertEqual(ember_transport.ensure_token(self.env_path), token)
        with open(self.env_path) as f:
            self.assertEqual(f.read().count("EMBER_TRANSPORT_TOKEN"), 1)

    def test_creates_dotenv_if_missing_and_survives_an_unwritable_one(self):
        token = ember_transport.ensure_token(self.env_path)
        self.assertTrue(os.path.exists(self.env_path))
        os.environ.pop(ember_transport.AUTH_TOKEN_ENV)
        bad = os.path.join(self.tmp.name, "no_such_dir", ".env")
        temp = ember_transport.ensure_token(bad)                # can't write: still returns a working token
        self.assertTrue(temp and temp != token)
        self.assertEqual(os.environ[ember_transport.AUTH_TOKEN_ENV], temp)


class ClientWaitsForServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_retries_until_the_server_comes_up(self):
        os.environ[ember_transport.AUTH_TOKEN_ENV] = TOKEN
        port = free_port()
        engines = VoiceEngines(tts=FakeTTS(), stt=FakeSTT(), vad_factory=FakeVAD)
        with tempfile.TemporaryDirectory() as tmp:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, os.path.join(ROOT, "voice_client.py"),
                "--url", f"ws://localhost:{port}", "--token", TOKEN, "--no-mic",
                "--save-audio", os.path.join(tmp, "o.wav"), "--wait", "15", "--exit-after", "2",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            await asyncio.sleep(1.5)                                   # client is already waiting...
            server = asyncio.create_task(ember_transport.run_transport_server(
                fake_turn, "localhost", port, voice_engines=engines))  # ...and only now does the server start
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            finally:
                server.cancel()
                try:
                    await server
                except (asyncio.CancelledError, Exception):
                    pass
        text = out.decode()
        self.assertIn("waiting up to 15s", text, text)
        self.assertIn("[client] connected", text, text)


class ClientHeadlessTest(TransportCase):
    """Drives voice_client.py itself (subprocess, --wav in / --save-audio out)."""

    async def test_client_wav_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_in, wav_out = os.path.join(tmp, "in.wav"), os.path.join(tmp, "out.wav")
            pcm = np.frombuffer(speech_bytes(20), dtype="<i2")
            with wave.open(wav_in, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm.tobytes())
            self.engines.stt.script.append("Hey Ember, hello from the client")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, os.path.join(ROOT, "voice_client.py"),
                "--url", f"ws://localhost:{self.port}", "--token", TOKEN,
                "--wav", wav_in, "--save-audio", wav_out, "--exit-after", "6",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = out.decode()
            self.assertIn("You (voice, wake): hello from the client", text, text)
            self.assertIn("Ember: You said hello from the client", text, text)
            with wave.open(wav_out, "rb") as w:
                self.assertEqual(w.getframerate(), 24000)
                self.assertGreater(w.getnframes(), 24000 * 0.25)


if __name__ == "__main__":
    unittest.main()
