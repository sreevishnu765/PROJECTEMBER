"""
voice_client.py
================
Command-line test client for Ember's voice pipeline — talks to a running
ember_transport.py exactly the way the frontend will, so you can exercise
wake word, voice mode, barge-in and spoken replies before any UI exists.

Live (real mic + speakers):
    pip install sounddevice websockets numpy
    python voice_client.py --token YOUR_TOKEN
  Half-duplex by default: Ember doesn't listen while she is speaking, so
  her own voice from your speakers can't be mistaken for you. That also
  means you can't talk over her (--barge-in enables that; use headphones
  or a client with echo cancellation such as the browser frontend).
  Useful flags:  --debug  shows what Whisper heard for every utterance, why it
                 was ignored/accepted, and how loud it was;  --states  shows
                 every listening/hearing/transcribing change (noisy).
  Audio setup:   --list-devices   then   --mic N / --speaker-device N   (Windows often
                 switches its default input when you plug in an aux cable);
                 --check-mic   records 4 s and tells you if the level is usable;
                 --meter   live level bar;   --gain 4 (fixed boost) or --no-auto-gain (auto gain is ON by default).
  Every voice turn prints a latency breakdown, and every reply prints
  when its first text arrived and in how many chunks — that's how to tell
  whether streaming is working.

Headless (no audio hardware — useful for testing/CI):
    python voice_client.py --token T --wav hey_ember.wav --save-audio reply.wav
  streams the WAV to the server as if it were the mic (real-time paced,
  16 kHz mono expected; other rates are resampled) and writes whatever
  Ember says back to reply.wav.

While running, type at the prompt:
    <text>            send a normal typed message
    /mode on|off      voice mode (no wake word needed while on)
    /voice NAME       heart | isabella | daniel
    /speaker on|off   also speak replies to typed messages
    /stop             stop speaking / cancel the current turn
    /quit
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time
import wave

import numpy as np
import websockets

MIC_RATE = 16000
PLAY_RATE = 24000
BLOCK = 640  # 40 ms of mic audio per frame
TURN = {"t0": None}   # when the current turn started (typed message sent / voice transcript received)
CONFIRM = {"pending": None, "interactive": False}   # a destructive-action approval waiting for the user's y/n


def read_dotenv_token(path: str) -> "str | None":
    """EMBER_TRANSPORT_TOKEN from a simple KEY=VALUE .env file, or None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    name, _, value = line.partition("=")
                    if name.strip() == "EMBER_TRANSPORT_TOKEN":
                        return value.strip().strip("'\"") or None
    except OSError:
        pass
    return None


def find_token(cli_value: "str | None") -> str:
    """--token, else the environment, else the project's .env (which the server writes if it had to
    generate a token)."""
    return (cli_value or os.environ.get("EMBER_TRANSPORT_TOKEN")
            or read_dotenv_token(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) or "")


def require_sounddevice():
    try:
        import sounddevice as sd
        return sd
    except (ImportError, OSError) as e:
        raise SystemExit(f"Audio needs the sounddevice package (pip install sounddevice) — {e}")


def level_stats(pcm: bytes) -> "tuple[float, float]":
    """(RMS in dBFS, peak 0..1) of a block of int16 PCM."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if x.size == 0:
        return -120.0, 0.0
    rms = float(np.sqrt(np.mean(x * x)))
    return 20.0 * float(np.log10(max(rms, 1e-6))), float(np.abs(x).max())


def apply_gain(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


class AutoGain:
    """Slow automatic gain for quiet laptop mics: aims at a steady speech level,
    never amplifies below a noise floor (so it doesn't boost hiss into 'speech'),
    caps the boost, and never lets the block clip."""

    def __init__(self, target_rms: float = 0.06, max_gain: float = 12.0, noise_floor_rms: float = 0.004):
        self.target, self.max_gain, self.floor = target_rms, max_gain, noise_floor_rms
        self.gain = 1.0

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if x.size == 0:
            return pcm
        rms, peak = float(np.sqrt(np.mean(x * x))), float(np.abs(x).max())
        if rms > self.floor:
            desired = min(self.max_gain, max(1.0, self.target / rms))
            self.gain += (desired - self.gain) * (0.5 if desired < self.gain else 0.1)
        if peak * self.gain > 0.98:
            self.gain = max(1.0, 0.98 / max(peak, 1e-6))
        return apply_gain(pcm, self.gain)


def pick_device(spec, devices, kind: str, preferred_hostapi: "int | None" = None):
    """Resolve --mic/--speaker (an index or a name fragment) to a device index.
    `devices` is sounddevice.query_devices()'s list of dicts."""
    if spec is None:
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        idx = int(spec)
    except (TypeError, ValueError):
        idx = None
    if idx is not None:
        if 0 <= idx < len(devices) and devices[idx][key] > 0:
            return idx
        raise SystemExit(f"Device {idx} isn't a valid {kind} device — run with --list-devices.")
    matches = [i for i, d in enumerate(devices) if d[key] > 0 and str(spec).lower() in d["name"].lower()]
    if not matches:
        raise SystemExit(f"No {kind} device matching {spec!r} — run with --list-devices.")
    for i in matches:
        if devices[i].get("hostapi") == preferred_hostapi:
            return i
    return matches[0]


def print_devices() -> None:
    sd = require_sounddevice()
    devices, apis = sd.query_devices(), sd.query_hostapis()
    din, dout = sd.default.device
    print("Audio devices  (IN = inputs, OUT = outputs; * = current Windows default)\n")
    for i, d in enumerate(devices):
        tags = []
        if d["max_input_channels"]:
            tags.append(f"IN{d['max_input_channels']}" + ("*" if i == din else ""))
        if d["max_output_channels"]:
            tags.append(f"OUT{d['max_output_channels']}" + ("*" if i == dout else ""))
        print(f"  [{i:2d}] {d['name'][:52]:52s} {apis[d['hostapi']]['name'][:14]:14s} {' '.join(tags)}")
    print("\nPick with:  --mic <number or part of the name>   --speaker-device <number or part of the name>")
    print("On Windows the same physical device appears once per host API (MME, DirectSound, WASAPI);")
    print("if one entry misbehaves, try another. If your laptop mic went quiet after plugging in the aux")
    print("cable, choose the built-in 'Microphone Array' explicitly instead of the default.")


class Player:
    """Playback queue with flush-on-stop and ducking. In --save-audio mode
    it just collects PCM and writes a WAV on close."""

    def __init__(self, save_path: "str | None" = None, device=None):
        self._device = device
        self._rate = PLAY_RATE
        self._save_rate = PLAY_RATE
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._save_path = save_path
        self._saved = bytearray()
        self._stream = None
        self.min_gen = 0
        self.gain = 1.0

    def start(self) -> None:
        if self._save_path:
            return
        sd = require_sounddevice()
        try:
            self._open(sd, PLAY_RATE)
        except Exception as first:
            # Some outputs (aux/USB/WASAPI) refuse 24 kHz; use the device's own rate and resample.
            rate = int(sd.query_devices(self._device, "output")["default_samplerate"])
            self._open(sd, rate)
            self._rate = rate
            print(f"[client] output device rejected 24 kHz ({first}); playing at {rate} Hz with resampling")

    def _open(self, sd, rate: int) -> None:
        self._stream = sd.RawOutputStream(
            samplerate=rate, channels=1, dtype="int16", blocksize=1024, callback=self._callback, device=self._device,
        )
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):
        need = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
            gain = self.gain
        if chunk and gain != 1.0:
            chunk = (np.frombuffer(chunk, dtype="<i2") * gain).astype("<i2").tobytes()
        outdata[:len(chunk)] = chunk
        if len(chunk) < need:
            outdata[len(chunk):] = b"\x00" * (need - len(chunk))

    def push(self, pcm: bytes, gen: int, src_rate: int = PLAY_RATE) -> None:
        """Queue audio for playback. `src_rate` is what the server synthesized at
        (24 kHz for Kokoro, 16-22 kHz for Piper); it's resampled to whatever
        rate the output device was opened at."""
        if gen < self.min_gen:
            return   # late stale chunk from before an interrupt
        if self._save_path:
            if not self._saved:
                self._save_rate = src_rate
            self._saved += pcm
        else:
            if src_rate != self._rate:
                x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
                n = max(1, int(len(x) * self._rate / src_rate))
                pcm = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("<i2").tobytes()
            with self._lock:
                self._buf += pcm

    def flush(self, gen: int) -> None:
        with self._lock:
            self._buf.clear()
        self.min_gen = max(self.min_gen, gen)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._save_path and self._saved:
            with wave.open(self._save_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self._save_rate)
                w.writeframes(bytes(self._saved))
            print(f"[client] wrote {len(self._saved) / (self._save_rate * 2):.1f}s of Ember's speech to {self._save_path}")


def load_wav_16k(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit("WAV must be 16-bit PCM")
    x = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1).astype("<i2")
    if rate != MIC_RATE:
        n = int(len(x) * MIC_RATE / rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("<i2")
    return x.tobytes()


async def send_wav(ws, path: str) -> None:
    pcm = load_wav_16k(path) + b"\x00\x00" * MIC_RATE * 2   # +2 s of silence so the VAD closes the utterance
    step = BLOCK * 2
    for i in range(0, len(pcm), step):
        await ws.send(pcm[i:i + step])
        await asyncio.sleep(BLOCK / MIC_RATE)
    print("[client] finished streaming WAV")


async def send_mic(ws, device=None, gain: float = 1.0, auto_gain: bool = False, meter: bool = False) -> None:
    sd = require_sounddevice()
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=200)

    def cb(indata, frames, time_info, status):
        try:
            loop.call_soon_threadsafe(q.put_nowait, bytes(indata))
        except asyncio.QueueFull:
            pass

    info = sd.query_devices(device, "input")
    apis = sd.query_hostapis()
    print(f"[client] microphone: {info['name']} ({apis[info['hostapi']]['name']})"
          f"{'  gain x%.1f' % gain if gain != 1.0 else ''}{'  auto-gain' if auto_gain else ''}")

    agc = AutoGain() if auto_gain else None
    win_peak, win_sq, win_n, quiet_seconds, last_warn = 0.0, 0.0, 0, 0, -99
    with sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=cb, device=device):
        while True:
            raw = await q.get()
            db, peak = level_stats(raw)              # level of what the DEVICE gave us, before any gain
            win_peak = max(win_peak, peak)
            win_sq += 10 ** (db / 10)
            win_n += 1
            if win_n >= 25:                          # ~1 second of 40 ms blocks
                mean_db = 10 * np.log10(max(win_sq / win_n, 1e-12))
                if meter:
                    bar = "#" * int(max(0, min(30, (mean_db + 70) / 70 * 30)))
                    print(f"\r[mic] {bar:<30s} {mean_db:6.1f} dBFS  peak {win_peak:4.2f}   ", end="", flush=True)
                quiet_seconds = quiet_seconds + 1 if win_peak < 0.004 else 0
                now = time.monotonic()
                if quiet_seconds >= 5 and now - last_warn > 30:
                    last_warn = now
                    print("\n[mic looks SILENT (peak %.4f) — wrong input device? Run --list-devices and pick one with --mic; "
                          "plugging in an aux cable often switches Windows to a 'headset mic' with nothing on it]" % win_peak)
                elif win_peak >= 0.99 and now - last_warn > 30:
                    last_warn = now
                    print("\n[mic is clipping — lower the input volume, or drop --gain]")
                win_peak, win_sq, win_n = 0.0, 0.0, 0
            if agc is not None:
                raw = agc.process(raw)
            elif gain != 1.0:
                raw = apply_gain(raw, gain)
            await ws.send(raw)


async def check_mic(device=None, seconds: float = 4.0, gain: float = 1.0) -> None:
    """Records a few seconds and says whether the level is usable — no server needed."""
    sd = require_sounddevice()
    info = sd.query_devices(device, "input")
    print(f"[check] recording {seconds:.0f}s from: {info['name']} — say something at normal volume, at your usual distance...")
    rec = sd.rec(int(seconds * MIC_RATE), samplerate=MIC_RATE, channels=1, dtype="int16", device=device)
    sd.wait()
    raw = apply_gain(rec.tobytes(), gain)
    blocks = [raw[i:i + BLOCK * 2] for i in range(0, len(raw) - BLOCK * 2, BLOCK * 2)]
    stats = [level_stats(b) for b in blocks]
    loud = sorted(db for db, _ in stats)[int(len(stats) * 0.9)]        # 90th-percentile block = your speech level
    quiet = sorted(db for db, _ in stats)[int(len(stats) * 0.1)]       # 10th percentile = the room's noise floor
    peak = max(pk for _, pk in stats)
    with wave.open("mic_check.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_RATE)
        w.writeframes(raw)
    print(f"[check] speech level {loud:.0f} dBFS · noise floor {quiet:.0f} dBFS · peak {peak:.2f}   (saved mic_check.wav — play it back)")
    if peak < 0.004:
        print("[check] SILENT. Wrong input device (or muted). Try --list-devices and --mic N.")
    elif loud < -45:
        print("[check] very quiet. Raise the input volume in Windows sound settings (auto-gain will also help).")
    elif loud - quiet < 12:
        print("[check] speech barely above the noise floor — move closer, reduce background noise, or try another input device.")
    elif peak > 0.98:
        print("[check] clipping — lower the input volume.")
    else:
        print("[check] level looks fine.")


async def stdin_loop(ws) -> None:
    """Reads the keyboard on a DAEMON thread (a plain to_thread(readline) is a non-daemon executor
    thread: Ctrl+C would hang until you pressed Enter). While an approval prompt is pending, the next
    line you type is the answer to it — not a chat message."""
    loop = asyncio.get_running_loop()
    lines: "asyncio.Queue[str | None]" = asyncio.Queue()

    def reader():
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(lines.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=reader, name="stdin-reader", daemon=True).start()
    CONFIRM["interactive"] = True
    while True:
        line = await lines.get()
        if line is None:
            return
        line = line.strip()
        if CONFIRM["pending"] is not None:
            request_id, CONFIRM["pending"] = CONFIRM["pending"], None
            approved = line.lower() in ("y", "yes")
            print(f"[client] {'approved' if approved else 'denied'}")
            await ws.send(json.dumps({"type": "confirm", "request_id": request_id, "approved": approved}))
            continue
        if not line:
            continue
        if line == "/quit":
            raise SystemExit(0)
        if line.startswith("/mode "):
            await ws.send(json.dumps({"type": "voice", "action": "mode", "on": line.split()[1] == "on"}))
        elif line.startswith("/voice "):
            await ws.send(json.dumps({"type": "voice", "action": "voice", "name": line.split()[1]}))
        elif line.startswith("/speaker "):
            await ws.send(json.dumps({"type": "voice", "action": "speaker", "on": line.split()[1] == "on"}))
        elif line == "/stop":
            await ws.send(json.dumps({"type": "voice", "action": "stop"}))
            await ws.send(json.dumps({"type": "cancel"}))
        else:
            print(f"You: {line}")
            TURN["t0"] = time.monotonic()
            await ws.send(json.dumps({"type": "message", "text": line}))


async def receive_loop(ws, player: Player, show_states: bool = False) -> None:
    last_state = None
    last_flags = None
    mid_reply = False
    first_chunk_at = None
    n_chunks = 0
    async for raw in ws:
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "audio_chunk":
            player.push(base64.b64decode(msg["data"]), msg.get("gen", 0), msg.get("sample_rate", PLAY_RATE))
        elif t == "audio_stop":
            player.flush(msg.get("gen", 0))
        elif t == "voice_duck":
            player.gain = 0.25 if msg.get("on") else 1.0
        elif t == "voice_state":
            key = (msg["state"], msg["voice_mode"], msg["awake"])
            if show_states and key != last_state:
                extra = (" · voice mode" if msg["voice_mode"] else "") + (" · awake" if msg["awake"] else "")
                print(f"\n[{msg['state']}{extra}]")
            last_state = key
            flags = (msg["voice_mode"], msg["awake"])
            if not show_states and flags != last_flags:
                last_flags = flags
                print(f"\n[{'voice mode ON' if msg['voice_mode'] else 'voice mode off'}{' · listening for a follow-up' if msg['awake'] else ''}]")
        elif t == "voice_heard":
            hint = "  <- VERY QUIET input" if msg.get("level_db", 0) < -50 else ("  <- mostly noise?" if msg.get("speech_ratio", 1) < 0.35 else "")
            print(f"\n[heard: {msg['text']!r} -> {msg['verdict']}  (stt {msg['stt_s']}s, level {msg.get('level_db')} dBFS, "
                  f"peak {msg.get('peak')}, speech {int(100 * msg.get('speech_ratio', 0))}%){hint}]")
        elif t == "voice_timing":
            print(f"\n[latency: endpoint {msg['endpoint_s']}s + stt {msg['stt_s']}s + turn-start {msg['turn_start_s']}s + "
                  f"LLM/search {msg['first_text_s']}s + tts {msg['tts_s']}s = {msg['total_s']}s until first audio]")
        elif t == "voice_warning":
            print(f"\n[WARNING: {msg['text']}]")
        elif t == "voice_event":
            print(f"\n[event: {msg['event']}]")
        elif t == "transcript":
            print(f"\nYou (voice, {msg['trigger']}): {msg['text']}")
            TURN["t0"], first_chunk_at, n_chunks = time.monotonic(), None, 0
        elif t == "status":
            print(f"\n[{msg['text']}]")
        elif t == "chunk":
            if first_chunk_at is None:
                first_chunk_at, n_chunks = time.monotonic(), 0
            n_chunks += 1
            if not mid_reply:
                print("Ember: ", end="", flush=True)
                mid_reply = True
            print(msg["text"], end="", flush=True)
        elif t == "done":
            if not mid_reply:
                print(f"Ember: {msg['text']}", end="")
            timing = ""
            t0 = TURN["t0"]
            if t0 is not None and first_chunk_at is not None:
                timing = f" (first text +{first_chunk_at - t0:.1f}s, {n_chunks} chunk(s), finished +{time.monotonic() - t0:.1f}s)"
            print(f"  [{msg['tag']}]{timing}")
            mid_reply = False
            TURN["t0"], first_chunk_at, n_chunks = None, None, 0
        elif t == "confirmation_required":
            if CONFIRM["interactive"]:
                CONFIRM["pending"] = msg["request_id"]
                print(f"\n[CONFIRM] Ember wants to run {msg['tool_name']} {msg.get('args')} — type y or n and press Enter: ", end="", flush=True)
            else:
                # headless mode (--wav): nobody can answer, so fail closed
                print(f"\n[CONFIRM] {msg['tool_name']} needs approval; denied automatically (no keyboard in this mode)")
                await ws.send(json.dumps({"type": "confirm", "request_id": msg["request_id"], "approved": False}))
        elif t == "voice_unavailable":
            print(f"\n[voice unavailable: {msg.get('reason')}]")
        elif t == "error":
            print(f"\n[error: {msg.get('message')}]")


async def connect_with_retry(url: str, wait_seconds: float):
    """Opens the WebSocket, retrying for `wait_seconds` (the server may still be starting up — model
    warm-up takes a while the first time) and turning "connection refused" into an explanation."""
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        try:
            return await websockets.connect(url)
        except (OSError, websockets.exceptions.WebSocketException) as e:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"Couldn't connect to {url} ({type(e).__name__}).\n"
                    "Nothing is listening there. Start the server first, in ANOTHER terminal (venv active):\n"
                    "    python run_transport.py\n"
                    "and wait for the line  [ember_transport] WebSocket server listening on ws://localhost:8765\n"
                    "If the server printed an error instead, fix that first. To see whether anything owns the port:\n"
                    "    netstat -ano | findstr 8765\n"
                    "(If the desktop app started its own backend, close the app or use the port/token it uses.)"
                )
            if not announced:
                announced = True
                print(f"[client] no server at {url} yet — waiting up to {wait_seconds:.0f}s "
                      f"(start it with: python run_transport.py)")
            await asyncio.sleep(1.0)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="ws://localhost:8765")
    ap.add_argument("--token", default=None, help="access token (default: EMBER_TRANSPORT_TOKEN, else the one in .env)")
    ap.add_argument("--voice", default=None, help="heart | isabella | daniel")
    ap.add_argument("--mode", action="store_true", help="start in voice mode (no wake word needed)")
    ap.add_argument("--no-mic", action="store_true", help="don't stream the mic (typed messages + spoken replies only)")
    ap.add_argument("--speaker", action="store_true", help="also speak replies to typed messages")
    ap.add_argument("--barge-in", action="store_true", help="let your voice interrupt Ember while she speaks (needs headphones or echo cancellation)")
    ap.add_argument("--debug", action="store_true", help="print what Whisper heard for EVERY utterance, and why it was ignored/accepted")
    ap.add_argument("--states", action="store_true", help="print every listening/hearing/transcribing state change (noisy)")
    ap.add_argument("--list-devices", action="store_true", help="list audio input/output devices and exit")
    ap.add_argument("--mic", default=None, help="input device: index or part of its name (see --list-devices)")
    ap.add_argument("--speaker-device", default=None, dest="speaker_dev", help="output device: index or part of its name")
    ap.add_argument("--gain", type=float, default=1.0, help="multiply mic level (e.g. 4 for a quiet laptop mic)")
    ap.add_argument("--no-auto-gain", action="store_true", help="turn OFF the automatic mic gain (on by default: up to x12, never clips, ignores the noise floor)")
    ap.add_argument("--meter", action="store_true", help="live mic level meter (for finding the right device/gain)")
    ap.add_argument("--check-mic", action="store_true", help="record 4 seconds, report the level, save mic_check.wav, and exit")
    ap.add_argument("--wait", type=float, default=20.0, help="seconds to keep retrying if the server isn't up yet (default 20)")
    ap.add_argument("--wav", help="stream this WAV file as the microphone (headless test mode)")
    ap.add_argument("--save-audio", help="write Ember's speech to this WAV instead of playing it")
    ap.add_argument("--exit-after", type=float, default=None, help="quit after N seconds")
    args = ap.parse_args()
    if args.list_devices:
        print_devices()
        return
    mic_dev = spk_dev = None
    if args.mic is not None or args.speaker_dev is not None or args.check_mic:
        sd = require_sounddevice()
        devs = sd.query_devices()
        mic_dev = pick_device(args.mic, devs, "input", sd.default.hostapi)
        spk_dev = pick_device(args.speaker_dev, devs, "output", sd.default.hostapi)
    if args.check_mic:
        await check_mic(mic_dev, gain=args.gain)
        return
    args.token = find_token(args.token)
    if not args.token:
        raise SystemExit("No token found. Start the server once (python run_transport.py) — it saves one to .env — "
                         "or pass --token.")

    if args.wav and args.exit_after is None:
        args.exit_after = len(load_wav_16k(args.wav)) / (MIC_RATE * 2) + 25.0
    if not args.wav and not args.no_mic:
        require_sounddevice()

    player = Player(save_path=args.save_audio, device=spk_dev)
    try:
        player.start()
    except Exception as e:
        raise SystemExit(f"Couldn't open the audio output ({e}). Try: pip install sounddevice — or use --save-audio.")

    ws_conn = await connect_with_retry(args.url, args.wait)
    async with ws_conn as ws:
        await ws.send(json.dumps({"type": "auth", "token": args.token}))
        first = json.loads(await ws.recv())
        if first.get("type") != "auth_ok":
            raise SystemExit("Authentication failed — check the token.")
        print("[client] connected")

        listening = not args.no_mic
        await ws.send(json.dumps({"type": "voice", "action": "listen", "on": listening}))
        if args.voice:
            await ws.send(json.dumps({"type": "voice", "action": "voice", "name": args.voice}))
        if args.barge_in:
            await ws.send(json.dumps({"type": "voice", "action": "barge_in", "on": True}))
        if args.debug:
            await ws.send(json.dumps({"type": "voice", "action": "debug", "on": True}))
        if args.mode:
            await ws.send(json.dumps({"type": "voice", "action": "mode", "on": True}))
        if args.speaker:
            await ws.send(json.dumps({"type": "voice", "action": "speaker", "on": True}))

        tasks = [asyncio.create_task(receive_loop(ws, player, show_states=args.states))]
        if args.wav:
            tasks.append(asyncio.create_task(send_wav(ws, args.wav)))
        elif listening:
            tasks.append(asyncio.create_task(
                send_mic(ws, device=mic_dev, gain=args.gain, auto_gain=(not args.no_auto_gain and args.gain == 1.0), meter=args.meter)))
        if not args.wav:
            tasks.append(asyncio.create_task(stdin_loop(ws)))
            print("[client] say 'Hey Ember, ...' — or type a message. /quit to exit. Use headphones.")

        try:
            if args.exit_after is not None:
                await asyncio.sleep(args.exit_after)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            player.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass   # SystemExit is deliberately NOT caught: its message is the friendly error
