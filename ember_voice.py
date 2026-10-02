"""
ember_voice.py
================
Ember's voice pipeline: always-on listening with wake-word activation, a
sticky "voice mode" that drops the wake word, and streamed spoken replies.

Design in one paragraph
-----------------------
The client (browser/Electron renderer) owns the microphone and the
speakers — that's what lets the browser's echo cancellation see both
sides of the audio — and streams raw mic PCM to this server over the
existing WebSocket. Everything heavy happens here: Silero VAD carves the
stream into utterances, faster-whisper transcribes each one, a
wake-phrase check (ember_voice_text.detect_wake) decides whether it was
meant for Ember, and if so the text goes into the SAME process_turn()
path a typed message uses. Reply text streams back through
process_turn()'s stream_callback; it's chunked into sentences, cleaned of
markdown, synthesized by Kokoro, and sent to the client as PCM chunks
while the rest of the reply is still being generated.

Nothing about a heard-but-ignored utterance leaves this process: ambient
speech is transcribed locally, checked for a wake phrase, and dropped.
Only text that passed the wake check (or arrived in voice mode / the
follow-up window) is ever sent to an LLM.

Wake logic, in priority order
-----------------------------
  wake phrase present        -> command (phrase stripped). A wake phrase
                                with nothing after it ("hey Ember") makes
                                Ember answer "Sir?" and listen for
                                WAKE_WINDOW_SECONDS without needing it again.
  voice mode on              -> every utterance is a command, wake phrase
                                optional. Turned on/off by "voice mode" /
                                "that's all" style phrases or the HUD toggle.
  follow-up window open      -> for FOLLOW_UP_SECONDS after Ember finishes
                                speaking a voice-originated reply, the next
                                utterance needs no wake phrase.
  otherwise                  -> ignored.
Barge-in: an accepted command that arrives while Ember is speaking or
thinking cancels the current reply first. While the user is talking over
Ember, the client is told to duck its volume immediately (before STT has
decided anything) and to restore it if the utterance turns out not to be
for Ember.

Stop-while-speaking: "Ember, stop.", "Ember, shut up.", and a bare "stop"
(also "quiet"/"shut up"/"cancel"/"that's enough"/"hold on"/"wait") all
interrupt her IMMEDIATELY whenever she's speaking or a turn is in flight —
no wake word or voice-mode/follow-up window required first, same courtesy
a person gets when interrupting someone talking. The primary mechanism is
a continuous acoustic keyword spotter for "stop" (see KWS_KEYWORDS/
_handle_acoustic_stop) that doesn't wait for a VAD segment to close —
VAD structurally CAN'T isolate a short "stop" from Ember's own continuous
simultaneous speech bleeding into an uncancelled mic, since there's no
silence gap to hang a segment boundary on until she next pauses. A second,
older mechanism (_handle_deaf_utterance) still gives short, cleanly-closed
bursts a quick transcribe-and-check pass — useful right after she finishes
talking, or as a fallback if the acoustic "stop" keyword's hand-tuned BPE
pieces don't match this particular KWS model (see the comment on that
keyword — it wasn't verified the way hey_ember/ember/wake_up were).

Speaker verification ("only my voice should activate it"): once a profile
is enrolled (see enroll_speaker.py), a genuine wake — "hey Ember", a bare
"Ember"/"wake up", or a trailing "...Ember" — is only honored if the
speaker's voiceprint matches the enrolled owner (see the SpeakerVerifier
section below for the fail-open contract on missing models/profiles).
This does NOT gate the stop phrases above (acoustic or transcribed). It DOES
gate continuations inside voice mode / a follow-up window once a profile is
enrolled (see _speaker_ok_continuation): the desktop app's mic button turns
voice mode on permanently, so without this any audio in the room (a video,
another person) would be accepted as a command.

Wire protocol (JSON over the existing WebSocket; mic audio is binary)
---------------------------------------------------------------------
Client -> server
  <binary frame>   mono PCM16 little-endian @ 16 kHz, any frame size
  {"type":"voice","action":"listen","on":true|false}   start/stop mic processing
  {"type":"voice","action":"mode","on":true|false}     voice mode (HUD toggle)
  {"type":"voice","action":"voice","name":"heart"|"isabella"|"daniel"}
  {"type":"voice","action":"speaker","on":true|false}  speak typed-message replies too
  {"type":"voice","action":"barge_in","on":true|false} let speech interrupt Ember while she talks.
                                                       Default OFF (half-duplex: mic audio is dropped while she
                                                       speaks). Turn on only with echo cancellation or headphones.
  {"type":"voice","action":"debug","on":true|false}    stream voice_heard for every transcribed utterance
  {"type":"voice","action":"stop"}                     stop speaking now
  {"type":"voice","action":"state"}                    re-send current voice_state
Server -> client
  {"type":"voice_state","state":"off|listening|hearing|transcribing|thinking|speaking",
   "listening":bool,"voice_mode":bool,"awake":bool,"speaker":bool,"voice":str}
  {"type":"voice_event","event":"wake"|"voice_mode_on"|"voice_mode_off"|"stopped"}
  {"type":"transcript","text":str,"trigger":"wake"|"voice_mode"|"follow_up"}
        -> the client should append a user message with this text and open
           a pending assistant message, exactly as if the user had typed it;
           chunk/status/done messages then arrive as usual.
  {"type":"audio_chunk","gen":int,"seq":int,"sample_rate":24000,"format":"pcm16","data":<base64>}
  {"type":"audio_stop","gen":int}  flush the playback queue and stop (interrupt / barge-in);
        discard any audio_chunk whose gen is lower than this one (a late stale chunk)
  {"type":"voice_unavailable","reason":str}  voice can't start (models/packages missing)
  {"type":"voice_warning","text":str}        e.g. speech synthesis is slower than real time here

Wake detection (see the constants above WakeSpotter): an acoustic keyword spotter, when installed.
"hey ember" is trusted anywhere in an utterance; bare "Ember" and "wake up" count only when they come
FIRST (they sound like "amber"/"wake up early" otherwise). Either way the audio after the wake word is
what gets transcribed. Transcript matching (detect_wake) remains as the fallback for everything else.
  {"type":"voice_duck","on":bool}          (barge-in mode only)
  {"type":"voice_heard","text":str,"verdict":str,"stt_s":float,"level_db":float,"peak":float,"speech_ratio":float}
        (debug on) what Whisper heard and what was done about it, plus the utterance's loudness.
        EMBER_VOICE_SAVE_CLIPS=<dir> on the server also saves each utterance as a WAV (last 40).
  {"type":"voice_timing","endpoint_s","stt_s","turn_start_s","first_text_s","tts_s","total_s"}
        per voice turn: where the time between "you stopped talking" and "first audio" went

Integration surface for ember_transport.py (see VoiceSession's docstring)
"""

import base64
import collections
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from ember_voice_text import (
    parse_confirmation_reply,
    SentenceChunker, detect_wake, is_stop_command, is_stt_garbage,
    looks_like_echo, name_aliases, parse_voice_mode_command, soft_wake_vetoed, strip_wake_residue,
)

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(_PROJECT_ROOT, "data", "voice_models")

KOKORO_MODEL_PATH = os.environ.get("EMBER_KOKORO_MODEL", os.path.join(MODELS_DIR, "kokoro-v1.0.onnx"))
KOKORO_VOICES_PATH = os.environ.get("EMBER_KOKORO_VOICES", os.path.join(MODELS_DIR, "voices-v1.0.bin"))
SILERO_MODEL_PATH = os.environ.get("EMBER_SILERO_MODEL", os.path.join(MODELS_DIR, "silero_vad.onnx"))
STT_MODEL_NAME = os.environ.get("EMBER_STT_MODEL", "base.en")
STT_PROMPT = os.environ.get("EMBER_STT_PROMPT", "")
# Biases Whisper towards spelling the wake word right. Costs nothing on real speech; set to "" to disable.
STT_HOTWORDS = os.environ.get("EMBER_STT_HOTWORDS", "Ember")
STT_BEAM = int(os.environ.get("EMBER_STT_BEAM", "1"))     # 1 = fastest; 5 is a little more accurate and slower

# TTS engine: "auto" (Piper if its voices are installed, else Kokoro), "piper", or "kokoro".
# Piper is ~5-10x faster on CPU; Kokoro sounds a little nicer but can run slower than real time
# on a laptop, which is what causes long pauses between sentences.
TTS_ENGINE = os.environ.get("EMBER_TTS", "auto").lower()
PIPER_DIR = os.environ.get("EMBER_PIPER_DIR", os.path.join(MODELS_DIR, "piper"))
PIPER_VOICES = {   # our voice name -> Piper voice file stem (see fetch_voice_models.py --piper)
    "daniel": "en_GB-alan-medium",
    "isabella": "en_GB-jenny_dioco-medium",
    "heart": "en_US-amy-medium",
}

TTS_SAMPLE_RATE = 24000
AUDIO_SLICE_SECONDS = 2   # max audio per audio_chunk message
MIC_SAMPLE_RATE = 16000


class VoiceModelsMissing(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Voices — Kokoro voice ids. Speeds are first guesses; tune by ear.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VoiceSpec:
    voice_id: str
    lang: str
    speed: float = 1.0


VOICES = {
    "heart": VoiceSpec("af_heart", "en-us", 1.0),
    "isabella": VoiceSpec("bf_isabella", "en-gb", 1.0),
    "daniel": VoiceSpec("bm_daniel", "en-gb", 1.0),
}
DEFAULT_VOICE = os.environ.get("EMBER_VOICE", "daniel").lower()
if DEFAULT_VOICE not in VOICES:
    DEFAULT_VOICE = "daniel"


@dataclass
class VoiceConfig:
    follow_up_seconds: float = 6.0     # window after a spoken reply where no wake phrase is needed
    wake_window_seconds: float = 8.0   # window after a bare "hey Ember"
    ack_phrase: str = "Sir?"           # spoken when addressed with nothing after the wake phrase
    speak_status: bool = True          # speak "Searching for that, sir..." notes (once per reply)
    debug: bool = field(default_factory=lambda: os.environ.get("EMBER_VOICE_DEBUG", "") not in ("", "0"))
    aliases: tuple = field(default_factory=name_aliases)
    vad_threshold: float = field(default_factory=lambda: float(os.environ.get("EMBER_VAD_THRESHOLD", "0.5")))
    vad_neg_threshold: "float | None" = None   # default: vad_threshold - 0.15
    # Half-duplex by default: while Ember is speaking (plus a short tail) incoming mic audio is
    # DROPPED, not transcribed. Without echo cancellation (e.g. laptop speakers + laptop mic)
    # Ember hears herself, burns the STT worker on her own sentences, and real commands queue
    # behind them. A client with real echo cancellation (the browser frontend) or headphones
    # turns barge-in on with {"type":"voice","action":"barge_in","on":true}.
    barge_in: bool = field(default_factory=lambda: os.environ.get("EMBER_VOICE_BARGE_IN", "") not in ("", "0"))
    deaf_tail_seconds: float = 0.4
    # Bursts with less speech than this are discarded before Whisper ever sees them. Lowered from 250 so a quick,
    # faint "Ember" survives (EMBER_VAD_MIN_SPEECH_MS to change it; door slams and clicks are still filtered).
    min_speech_ms: int = field(default_factory=lambda: int(os.environ.get("EMBER_VAD_MIN_SPEECH_MS", "160")))
    end_silence_ms: int = field(default_factory=lambda: int(os.environ.get("EMBER_VAD_END_SILENCE_MS", "700")))
    max_utterance_s: float = 20.0
    pre_roll_ms: int = 320
    # How long a barge-in waits for a cancelled turn to unwind before giving up and saying
    # "still finishing" instead. Was 8.0 — too short: this same voice session's own transcript
    # shows ordinary grounded replies taking 26-37s (search + a slower fallback tier), so a new
    # command spoken during one of those was routinely told to "say that again" even though
    # nothing was actually stuck, just legitimately slow. Raised to give normal slow turns room
    # to actually finish cancelling; a turn that's genuinely wedged (see llm_client.py's
    # GEMINI_REQUEST_TIMEOUT_MS fix) now fails on its own well before this anyway, so this number
    # is about UX patience for real latency, not a safety net for a hang anymore.
    busy_wait_seconds: float = 20.0

    def __post_init__(self):
        if self.vad_neg_threshold is None:
            self.vad_neg_threshold = max(0.05, self.vad_threshold - 0.15)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def normalize_for_stt(audio: np.ndarray, target_peak: float = 0.7, max_gain: float = 10.0) -> np.ndarray:
    """Whisper's log-mel front end isn't fully level-invariant; quiet laptop-mic
    speech (peak ~0.1) transcribes measurably worse than the same speech at a
    healthy level. Lift the utterance to a target peak (never more than max_gain,
    never attenuating)."""
    audio = np.asarray(audio, dtype=np.float32)
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak < 1e-4 or peak >= target_peak:
        return audio
    return audio * min(max_gain, target_peak / peak)


def float_to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


# ---------------------------------------------------------------------------
# VAD + utterance segmentation
# ---------------------------------------------------------------------------

class SileroVAD:
    """Silero VAD v5 via onnxruntime directly (numpy only — no torch).
    One shared onnxruntime session per model file (run() is thread-safe);
    the recurrent state lives on this wrapper, so each VoiceSession gets
    its own."""

    FRAME = 512
    CONTEXT = 64
    _sessions: "dict[str, object]" = {}
    _sessions_lock = threading.Lock()

    def __init__(self, model_path: str = SILERO_MODEL_PATH):
        self._session = self._get_session(model_path)
        self.reset()

    @classmethod
    def _get_session(cls, path: str):
        with cls._sessions_lock:
            if path not in cls._sessions:
                if not os.path.exists(path):
                    raise VoiceModelsMissing(f"Silero VAD model not found at {path} — run fetch_voice_models.py.")
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 1
                opts.inter_op_num_threads = 1
                sess = ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
                names = {i.name for i in sess.get_inputs()}
                if not {"input", "state", "sr"} <= names:
                    raise VoiceModelsMissing(
                        f"{path} isn't a Silero VAD v5 model (inputs: {sorted(names)}) — re-run fetch_voice_models.py."
                    )
                cls._sessions[path] = sess
            return cls._sessions[path]

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, state = self._session.run(None, {"input": x, "state": self._state, "sr": np.array(MIC_SAMPLE_RATE, dtype=np.int64)})
        self._state = state
        self._context = x[:, -self.CONTEXT:]
        return float(out[0][0])


@dataclass
class SegmentEvent:
    kind: str                              # "start" | "end"
    audio: "np.ndarray | None" = None      # float32 @ 16 kHz, set on "end"
    speech_ratio: float = 0.0              # fraction of the segment's frames the VAD scored as speech — low means noise
    start_sample: int = 0                  # absolute position (in samples fed since the session began) of audio[0]


class UtteranceSegmenter:
    """Turns a continuous PCM16 stream into utterances using a VAD with
    hysteresis (start above `threshold`, keep going until below
    `neg_threshold` for `end_silence_ms`). Short bursts under
    `min_speech_ms` (door slams, keyboard clicks) are discarded. A short
    pre-roll is kept so the first syllable isn't clipped by the VAD's
    onset lag — matters for a wake word, which IS the first syllable."""

    def __init__(self, vad, cfg: VoiceConfig):
        self._vad = vad
        self._cfg = cfg
        self._frame_ms = 1000.0 * SileroVAD.FRAME / MIC_SAMPLE_RATE
        self._pre_roll_frames = max(1, int(cfg.pre_roll_ms / self._frame_ms))
        self._end_frames = max(1, int(cfg.end_silence_ms / self._frame_ms))
        self._max_frames = int(cfg.max_utterance_s * 1000 / self._frame_ms)
        self._start_frames = 2
        self._carry = np.zeros(0, dtype=np.int16)
        self.samples_fed = 0        # every sample ever given to feed() — the "clock" wake-word detections are placed on
        self._frame_abs = 0
        self._seg_start = 0
        self.reset()

    def reset(self) -> None:
        self._vad.reset()
        self._carry = np.zeros(0, dtype=np.int16)
        self._pre = collections.deque(maxlen=self._pre_roll_frames)
        self._buf: "list[np.ndarray]" = []
        self._in_speech = False
        self._run = 0
        self._speech_frames = 0
        self._silence = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def feed(self, pcm16: bytes) -> "list[SegmentEvent]":
        if len(pcm16) % 2:
            pcm16 = pcm16[:-1]
        data = np.frombuffer(pcm16, dtype="<i2")
        base = self.samples_fed - self._carry.size          # absolute index of data[0] after the carry is prepended
        self.samples_fed += len(data)
        if self._carry.size:
            data = np.concatenate([self._carry, data])
        n = (len(data) // SileroVAD.FRAME) * SileroVAD.FRAME
        self._carry = data[n:].copy()
        events: "list[SegmentEvent]" = []
        for i in range(0, n, SileroVAD.FRAME):
            frame = data[i:i + SileroVAD.FRAME].astype(np.float32) / 32768.0
            self._frame_abs = base + i
            events += self._step(frame)
        return events

    def _step(self, frame: np.ndarray) -> "list[SegmentEvent]":
        p = self._vad.prob(frame)
        cfg = self._cfg
        if not self._in_speech:
            self._pre.append(frame)
            if p >= cfg.vad_threshold:
                self._run += 1
                if self._run >= self._start_frames:
                    self._in_speech = True
                    self._buf = list(self._pre)
                    self._seg_start = self._frame_abs - (len(self._buf) - 1) * SileroVAD.FRAME
                    self._speech_frames = self._run
                    self._silence = 0
                    return [SegmentEvent("start")]
            else:
                self._run = 0
            return []

        self._buf.append(frame)
        if p >= cfg.vad_threshold:
            self._speech_frames += 1
            self._silence = 0
        elif p < cfg.vad_neg_threshold:
            self._silence += 1
        # between the two thresholds: neither speech nor silence — hold state

        if self._silence >= self._end_frames or len(self._buf) >= self._max_frames:
            keep_tail = int(250 / self._frame_ms)
            trim = max(0, self._silence - keep_tail)
            frames = self._buf[: len(self._buf) - trim] if trim else self._buf
            long_enough = self._speech_frames * self._frame_ms >= cfg.min_speech_ms
            audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
            ratio = self._speech_frames / max(1, len(frames))
            start_abs = self._seg_start
            self.reset_after_segment()
            if long_enough:
                return [SegmentEvent("end", audio, ratio, start_abs)]
            return [SegmentEvent("end", None)]  # too short: tells the session speech stopped, nothing to transcribe
        return []

    def reset_after_segment(self) -> None:
        self._vad.reset()
        self._pre.clear()
        self._buf = []
        self._in_speech = False
        self._run = 0
        self._speech_frames = 0
        self._silence = 0


# ---------------------------------------------------------------------------
# Acoustic wake word: "hey ember"
# ---------------------------------------------------------------------------
# Whisper is a transcriber, and transcribers are bad at rare names ("Hey Ember" came out as
# "Hey, I'm up", "Hey M.", "Remember what...", or lost the name entirely). A keyword spotter
# listens for the SOUND of a phrase instead. sherpa-onnx's streaming KWS model (3.3M params,
# ~1.5% of real time on one CPU core) takes phrases as plain text, no training needed.
#
# Measured on synthetic speech (14 voices x clean / quiet / noisy / fast, 30 confusable phrases x 6 voices as
# negatives; defaults below = the best setting found: score 1.5, threshold 0.15):
#   "hey ember"  90% detected (95% clean; the misses are mostly noisy/fast speech), 0 false alarms in 720
#   "ember"      94% detected, but ~12% false alarms — look-alike words ("remember", "December", "amber")
#   "wake up"    89% detected, 0 false alarms when it must come first in the utterance
# So "hey ember" is trusted anywhere; "ember" / "wake up" are "soft": they only count at the START of an
# utterance, and a soft hit is confirmed with a short transcription of that first second to veto look-alikes
# (ember_voice_text.soft_wake_vetoed). Real speech from a real mic will differ — voice_tune.py measures and
# tunes all of this on your own recordings.
KWS_DIR = os.environ.get("EMBER_KWS_DIR", os.path.join(MODELS_DIR, "kws"))
KWS_ENABLED = os.environ.get("EMBER_KWS", "1") not in ("0", "false", "off")
KWS_THRESHOLD = float(os.environ.get("EMBER_KWS_THRESHOLD", "0.15"))   # "hey ember": lower = more sensitive
KWS_SCORE = float(os.environ.get("EMBER_KWS_SCORE", "1.5"))         # "hey ember": keyword boost
# "Soft" phrases: bare "Ember" and "wake up". Acoustically they also match "amber", "timber", "wake up early",
# so a soft hit only counts when it ENDS within KWS_SOFT_MAX_FROM_START_S of the start of the utterance —
# i.e. when it is what you said first, the way a wake word is used — and never in the middle of a sentence.
KWS_SOFT_ENABLED = os.environ.get("EMBER_KWS_SOFT", "1") not in ("0", "false", "off")
KWS_SOFT_THRESHOLD = float(os.environ.get("EMBER_KWS_SOFT_THRESHOLD", "0.15"))
KWS_SOFT_SCORE = float(os.environ.get("EMBER_KWS_SOFT_SCORE", "1.5"))
KWS_SOFT_MAX_FROM_START_S = 1.8      # includes the ~0.3 s of pre-roll the segmenter keeps before speech
KWS_KEYWORDS = {                                                      # BPE pieces of each phrase for this model
    "hey_ember": "\u2581HE Y \u2581E M BER",
    "ember": "\u2581E M BER",
    "wake_up": "\u2581WA KE \u2581UP",
    # Best-effort guess, NOT verified against this model's tokens.txt the way the three phrases
    # above were (see the "Measured on synthetic speech" note above — those numbers came from
    # actual testing; this one didn't, because there's no way to run the model from here). First
    # attempt ("\u2581ST OP") failed outright — a real, reproduced startup crash confirmed "ST" IS
    # a real token in this vocabulary but "OP" is NOT ("Cannot find ID for token OP"). Revised to
    # fall back to single letters for the rest of the word, since every BPE vocabulary trained on
    # English text has each individual letter as a base token by construction — "O" and "P" are
    # about as safe a guess as exists. WakeSpotter validates every piece against the real
    # tokens.txt below BEFORE ever handing it to sherpa_onnx now (see _load_kws_tokens) — a wrong
    # guess here can no longer crash the process the way the first attempt did; it just logs which
    # piece doesn't exist and quietly excludes "stop" (falling back to _handle_deaf_utterance's
    # slower transcribe-a-short-burst path) instead of building a keyword file sherpa_onnx would
    # abort on. If it's still wrong, that log line will say exactly which piece to fix next.
    "stop": "\u2581ST O P",
}
KWS_STRONG = ("hey_ember", "stop")   # trusted anywhere in an utterance
KWS_FILES = {
    "tokens": "tokens.txt",
    "encoder": "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder": "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner": "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
}


def _load_kws_tokens(model_dir: str) -> "set[str] | None":
    """Reads tokens.txt and returns the set of valid token strings, or None if it can't be read
    (missing/malformed — callers then skip validation and let sherpa_onnx's own checks run,
    since with nothing to validate against we're no worse off than before this existed).

    sherpa-onnx's tokens.txt is one "<token> <id>" pair per line. This is what makes it possible
    to catch an invalid keyword piece BEFORE ever constructing the KeywordSpotter — see
    WakeSpotter's own docstring/comments for why that matters: the C++ check that would otherwise
    catch this is fatal (aborts the whole process), not a catchable Python exception."""
    path = os.path.join(model_dir, KWS_FILES["tokens"])
    try:
        tokens: "set[str]" = set()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if not line:
                    continue
                parts = line.split()
                if parts:
                    tokens.add(parts[0])
        return tokens or None
    except OSError:
        return None


def wake_window(start_sample: int, end_sample: int) -> "tuple[int, int]":
    """Absolute sample range in which a detection is attributed to an utterance that spans start..end."""
    return start_sample - int(0.3 * MIC_SAMPLE_RATE), end_sample + int(0.6 * MIC_SAMPLE_RATE)


def pick_wake_hit(hits_in_window, start_sample: int) -> "tuple[int, str] | None":
    """THE selection rule (used by the live session AND by voice_tune.py, so tuning can't disagree with
    production): of the detections attributed to an utterance, a strong phrase ("hey ember") counts
    wherever it fell; a soft one ("ember", "wake up") counts only if it ended within
    KWS_SOFT_MAX_FROM_START_S of the utterance start. Strong beats soft; otherwise the earliest wins."""
    soft_limit = start_sample + int(KWS_SOFT_MAX_FROM_START_S * MIC_SAMPLE_RATE)
    eligible = [h for h in hits_in_window if h[1] in KWS_STRONG or h[0] <= soft_limit]
    strong = [h for h in eligible if h[1] in KWS_STRONG]
    pool = strong or eligible
    return min(pool) if pool else None


def kws_ready() -> bool:
    return (KWS_ENABLED and _module_available("sherpa_onnx")
            and all(os.path.exists(os.path.join(KWS_DIR, f)) for f in KWS_FILES.values()))


class WakeSpotter:
    """The shared sherpa-onnx keyword-spotting model. Each VoiceSession gets its own WakeStream."""

    def __init__(self, model_dir: str = KWS_DIR, threshold: float = KWS_THRESHOLD, score: float = KWS_SCORE,
                 soft_threshold: float = KWS_SOFT_THRESHOLD, soft_score: float = KWS_SOFT_SCORE,
                 soft: bool = KWS_SOFT_ENABLED, keywords_path: "str | None" = None):
        import sherpa_onnx
        for f in KWS_FILES.values():
            if not os.path.exists(os.path.join(model_dir, f)):
                raise VoiceModelsMissing(f"Wake-word model file missing: {os.path.join(model_dir, f)} — run fetch_voice_models.py --kws")
        keywords_path = keywords_path or os.path.join(model_dir, "ember_keywords.txt")

        # Real, reproduced crash this guards against: sherpa_onnx's C++ keyword-graph builder
        # calls a FATAL check (LOG(FATAL) — not a catchable Python exception, not even a normal
        # C++ exception) the instant a keyword line references a token that isn't in tokens.txt.
        # It aborts the entire process — "Cannot find ID for token OP ... Encode keywords failed"
        # followed by the whole server dying, taking every connected client down with it. A
        # try/except around KeywordSpotter(...) construction CANNOT catch this — there's nothing
        # left running to catch it — so the only safe approach is validating every piece against
        # the model's actual vocabulary BEFORE ever handing it to sherpa_onnx, not attempting the
        # risky keyword and reacting if it fails.
        valid_tokens = _load_kws_tokens(model_dir)
        invalid = {
            name: [p for p in pieces.split() if valid_tokens is not None and p not in valid_tokens]
            for name, pieces in KWS_KEYWORDS.items()
        }
        exclude = {name for name, bad in invalid.items() if bad}
        if exclude:
            for name in exclude:
                print(f"[ember_voice] Skipping keyword '{name}' — token(s) {invalid[name]} in {KWS_KEYWORDS[name]!r} "
                      f"aren't in this model's tokens.txt (checked {os.path.join(model_dir, KWS_FILES['tokens'])}). "
                      f"This keyword needed re-tuning against the real vocabulary; see KWS_KEYWORDS's comments.")
        elif valid_tokens is None:
            print(f"[ember_voice] Couldn't read tokens.txt to pre-validate keyword pieces — proceeding as-is. "
                  f"If the server crashes on startup with an 'Encode keywords failed' message, that's why.")

        with open(keywords_path, "w", encoding="utf-8") as f:     # explicit UTF-8: the BPE marker is not ASCII
            for name, pieces in KWS_KEYWORDS.items():
                if name in exclude:
                    continue
                if name in KWS_STRONG:
                    f.write(f"{pieces} :{score} #{threshold} @{name}\n")
                elif soft:
                    f.write(f"{pieces} :{soft_score} #{soft_threshold} @{name}\n")

        self._model = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(model_dir, KWS_FILES["tokens"]),
            encoder=os.path.join(model_dir, KWS_FILES["encoder"]),
            decoder=os.path.join(model_dir, KWS_FILES["decoder"]),
            joiner=os.path.join(model_dir, KWS_FILES["joiner"]),
            keywords_file=keywords_path, num_threads=1, keywords_score=score, keywords_threshold=threshold,
        )
        self.stop_keyword_active = "stop" not in exclude
        self._lock = threading.Lock()

    def new_stream(self) -> "WakeStream":
        return WakeStream(self)


class WakeStream:
    """Feed raw mic PCM; get back the absolute sample positions (in the same 'samples fed' clock as
    UtteranceSegmenter.samples_fed) at which a wake phrase was detected. Detection lands within
    ~0.2 s of the end of the phrase — which is close enough to cut the audio right after the wake
    word and hand only the command to Whisper."""

    def __init__(self, spotter: WakeSpotter):
        self._sp = spotter
        with spotter._lock:
            self._stream = spotter._model.create_stream()
        self.samples_fed = 0

    @property
    def stop_keyword_active(self) -> bool:
        return self._sp.stop_keyword_active

    def accept(self, pcm16: bytes) -> "list[tuple[int, str]]":
        """[(absolute_sample_position, keyword_name), ...] for anything detected in this chunk."""
        if len(pcm16) % 2:
            pcm16 = pcm16[:-1]
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        hits: "list[tuple[int, str]]" = []
        with self._sp._lock:
            self._stream.accept_waveform(MIC_SAMPLE_RATE, samples)
            self.samples_fed += len(samples)
            model = self._sp._model
            while model.is_ready(self._stream):
                model.decode_stream(self._stream)
                name = model.get_result(self._stream)
                if name:
                    hits.append((self.samples_fed, str(name)))
                    model.reset_stream(self._stream)
        return hits


# ---------------------------------------------------------------------------
# Speaker verification: "only my voice should activate it"
# ---------------------------------------------------------------------------
# The wake-word check above (KWS acoustic spotter + detect_wake) answers
# "was that Ember's name?" — it has no idea WHOSE voice said it. Anyone in
# earshot who says "hey Ember" (or is close enough for the KWS/soft-veto
# tolerances) currently wakes her. This closes that: a speaker-embedding
# model (sherpa-onnx's SpeakerEmbeddingExtractor) turns the wake-utterance's
# audio into a fixed-size voiceprint, compared by cosine similarity against
# one or more reference voiceprints enrolled ahead of time (see
# enroll_speaker.py). A wake whose voiceprint doesn't clear
# SPEAKER_MATCH_THRESHOLD against every enrolled sample is treated exactly
# like a failed wake-word match — dropped, logged, nothing sent to the LLM.
#
# Fails OPEN, deliberately, on every "can't tell" case (no model installed,
# no profile enrolled yet, or the clip is too short/quiet to embed
# reliably) — never on a mismatch. A checkout that never ran
# enroll_speaker.py behaves exactly as it did before this feature existed;
# it is never silently locked out by a half-configured model. Only an
# ACTUAL enrolled profile plus an ACTUAL below-threshold embedding refuses
# a wake. This mirrors the wake-word system's own honest-degradation
# contract (transcript matching if the acoustic model is missing) rather
# than turning a missing optional model into a hard failure.
#
# Also applied to voice-mode / follow-up continuations (see
# _speaker_ok_continuation) — stricter there: once enrolled, a clip too short
# to embed is refused rather than failing open, since voice mode is always
# awake and would otherwise let any short ambient line through. Deliberately
# NOT applied to the
# half-duplex "stop" interrupt in _handle_deaf_utterance (that clip is
# usually well under a second — too short to embed reliably — and a
# spurious stop from a stranger's voice is a cheap, instantly-recoverable
# mistake, unlike a full wake letting someone else start a conversation).
SPEAKER_MODEL_PATH = os.environ.get(
    "EMBER_SPEAKER_MODEL", os.path.join(MODELS_DIR, "nemo_en_speakerverification_speakernet.onnx")
)
SPEAKER_PROFILE_PATH = os.environ.get("EMBER_SPEAKER_PROFILE", os.path.join(MODELS_DIR, "speaker_profile.json"))
SPEAKER_MATCH_THRESHOLD = float(os.environ.get("EMBER_SPEAKER_THRESHOLD", "0.5"))  # cosine sim; first guess, not tuned
SPEAKER_MIN_AUDIO_S = 0.6   # below this an embedding is unreliable enough that "can't tell" beats "not you"


def speaker_verification_ready() -> bool:
    """Cheap check — never loads the model. Used by voice_status() and by
    enroll_speaker.py before it tries to record anything."""
    return _module_available("sherpa_onnx") and os.path.exists(SPEAKER_MODEL_PATH)


class SpeakerVerifier:
    """Wraps sherpa-onnx's speaker-embedding extractor. One shared instance
    per process (see VoiceEngines), same reasoning as WakeSpotter: loading
    the model is the expensive part, embedding a clip is cheap.

    `enrolled` is False (and every wake passes unchecked) until
    enroll_speaker.py has actually written at least one voiceprint to
    SPEAKER_PROFILE_PATH — voice-lock is opt-in by having a profile, not by
    a separate on/off flag that could be left in an inconsistent state."""

    def __init__(self, model_path: str = SPEAKER_MODEL_PATH, profile_path: str = SPEAKER_PROFILE_PATH):
        self._profile_path = profile_path
        self._profile: "list[list[float]]" = []
        self._extractor = None
        self._load_profile()
        if speaker_verification_ready():
            try:
                import sherpa_onnx
                config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=1)
                self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            except Exception as e:
                print(f"[ember_voice] Speaker-verification model failed to load ({e}) — wake will accept any voice.")
        elif self._profile:
            print(f"[ember_voice] A speaker profile exists but the verification model isn't at {model_path} "
                  f"— wake will accept any voice until it's installed.")

    def _load_profile(self) -> None:
        if not os.path.exists(self._profile_path):
            return
        try:
            with open(self._profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and all(isinstance(v, list) for v in data):
                self._profile = data
        except (OSError, ValueError) as e:
            print(f"[ember_voice] Couldn't read speaker profile ({e}) — treating as not enrolled.")

    @property
    def enrolled(self) -> bool:
        return bool(self._profile) and self._extractor is not None

    def embed(self, audio: np.ndarray) -> "list[float] | None":
        if self._extractor is None or audio is None or len(audio) < int(SPEAKER_MIN_AUDIO_S * MIC_SAMPLE_RATE):
            return None
        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=MIC_SAMPLE_RATE, waveform=np.asarray(audio, dtype=np.float32))
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            return list(self._extractor.compute(stream))
        except Exception as e:
            print(f"[ember_voice] Speaker embedding failed: {e}")
            return None

    def enroll(self, audio: np.ndarray) -> bool:
        """Adds one reference clip's voiceprint and persists the profile.
        Called from enroll_speaker.py, never from a live session."""
        vec = self.embed(audio)
        if vec is None:
            return False
        self._profile.append(vec)
        try:
            os.makedirs(os.path.dirname(self._profile_path), exist_ok=True)
            with open(self._profile_path, "w", encoding="utf-8") as f:
                json.dump(self._profile, f)
        except OSError as e:
            print(f"[ember_voice] Couldn't save speaker profile: {e}")
            self._profile.pop()
            return False
        return True

    def matches(self, audio: np.ndarray) -> "bool | None":
        """True/False once enrolled and the clip embeds cleanly; None if
        verification genuinely can't be performed right now (see the
        fail-open reasoning above) — callers must treat None as 'let it
        through', not as a mismatch."""
        if not self.enrolled:
            return None
        vec = self.embed(audio)
        if vec is None:
            return None
        best = max(self._cosine(vec, ref) for ref in self._profile)
        return best >= SPEAKER_MATCH_THRESHOLD

    @staticmethod
    def _cosine(a, b) -> float:
        a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
        na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))


# ---------------------------------------------------------------------------
# TTS / STT engines (lazy, shared across sessions)
# ---------------------------------------------------------------------------

class KokoroTTS:
    name = "kokoro"

    def __init__(self, model_path: str = KOKORO_MODEL_PATH, voices_path: str = KOKORO_VOICES_PATH):
        self._model_path = model_path
        self._voices_path = voices_path
        self._kokoro = None
        self._lock = threading.Lock()
        self._cache: "dict[tuple[str, str], tuple[np.ndarray, int]]" = {}

    def _load(self):
        if self._kokoro is None:
            for p in (self._model_path, self._voices_path):
                if not os.path.exists(p):
                    raise VoiceModelsMissing(f"Kokoro file not found: {p} — run fetch_voice_models.py.")
            from kokoro_onnx import Kokoro
            self._kokoro = Kokoro(self._model_path, self._voices_path)
        return self._kokoro

    def synthesize(self, text: str, voice_key: str) -> "tuple[np.ndarray, int]":
        spec = VOICES.get(voice_key) or VOICES[DEFAULT_VOICE]
        cache_key = (text, voice_key)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        with self._lock:
            kokoro = self._load()
            samples, sr = kokoro.create(text, voice=spec.voice_id, speed=spec.speed, lang=spec.lang)
        samples = np.asarray(samples, dtype=np.float32)
        if samples.size and (not np.isfinite(samples).all() or float(np.abs(samples).max()) > 8.0):
            # Observed with the int8 Kokoro build on some onnxruntime versions:
            # finite but astronomically large samples that trim to silence.
            raise RuntimeError(
                "Kokoro produced invalid audio — if EMBER_KOKORO_MODEL points at an int8 model, use the fp32 file."
            )
        if len(text) <= 40:   # short stock phrases only ("Sir?", "Voice mode off, sir.")
            if len(self._cache) >= 32:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (samples, int(sr))
        return samples, int(sr)


class PiperTTS:
    """Piper (VITS, onnxruntime) — much faster than Kokoro on CPU. One model file
    per voice, loaded on first use. If the requested voice isn't installed but
    another is, that one is used (with a one-time note) rather than going silent."""

    name = "piper"

    def __init__(self, voices_dir: str = PIPER_DIR, voice_map: "dict | None" = None):
        self._dir = voices_dir
        self._map = dict(voice_map or PIPER_VOICES)
        self._voices: "dict[str, object]" = {}
        self._lock = threading.Lock()
        self._cache: "dict[tuple[str, str], tuple[np.ndarray, int]]" = {}
        self._noted_fallback = False

    def _model_path(self, key: str) -> str:
        return os.path.join(self._dir, self._map[key] + ".onnx")

    def installed_voices(self) -> "list[str]":
        return [k for k in self._map if os.path.exists(self._model_path(k))]

    def _resolve(self, key: str) -> str:
        installed = self.installed_voices()
        if not installed:
            raise VoiceModelsMissing(f"No Piper voices found in {self._dir} — run: python fetch_voice_models.py --piper")
        if key in installed:
            return key
        if not self._noted_fallback:
            self._noted_fallback = True
            print(f"[ember_voice] Piper voice for {key!r} isn't installed; using {installed[0]!r} "
                  f"(run fetch_voice_models.py --piper for the rest).")
        return installed[0]

    def synthesize(self, text: str, voice_key: str) -> "tuple[np.ndarray, int]":
        cache_key = (text, voice_key)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        with self._lock:
            key = self._resolve(voice_key)
            if key not in self._voices:
                from piper import PiperVoice
                model = self._model_path(key)
                cfg = model + ".json"
                self._voices[key] = PiperVoice.load(model, cfg if os.path.exists(cfg) else None)
            voice = self._voices[key]
            parts = [np.asarray(chunk.audio_float_array, dtype=np.float32) for chunk in voice.synthesize(text)]
            sr = int(voice.config.sample_rate)
        samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        if peak > 0.9:      # Piper output is peak-normalized to full scale; leave headroom for ducking/resampling
            samples = samples * (0.9 / peak)
        if len(text) <= 40:
            if len(self._cache) >= 32:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (samples, sr)
        return samples, sr


def _module_available(name: str) -> bool:
    import importlib.util as iu
    return iu.find_spec(name) is not None


def piper_ready() -> bool:
    return _module_available("piper") and bool(PiperTTS().installed_voices())


def kokoro_ready() -> bool:
    return _module_available("kokoro_onnx") and all(os.path.exists(p) for p in (KOKORO_MODEL_PATH, KOKORO_VOICES_PATH))


def make_tts():
    """Picks the TTS engine per EMBER_TTS (default auto: Piper if installed, else Kokoro)."""
    if TTS_ENGINE == "piper":
        return PiperTTS()
    if TTS_ENGINE == "kokoro":
        return KokoroTTS()
    return PiperTTS() if piper_ready() else KokoroTTS()


class WhisperSTT:
    def __init__(self, model_name: str = STT_MODEL_NAME, prompt: str = STT_PROMPT,
                 hotwords: "str | None" = None, beam_size: "int | None" = None):
        """hotwords/beam_size default to the EMBER_STT_* settings; pass "" / a number to override (voice_tune.py
        compares settings this way)."""
        self._name = model_name
        self._prompt = prompt or None
        self._hotwords = STT_HOTWORDS if hotwords is None else hotwords
        self._beam = STT_BEAM if beam_size is None else beam_size
        self._model = None
        self._lock = threading.Lock()

    def set_options(self, hotwords: "str | None" = None, beam_size: "int | None" = None) -> None:
        """Change hotwords/beam without reloading the model (used by voice_tune.py's comparisons)."""
        if hotwords is not None:
            self._hotwords = hotwords
        if beam_size is not None:
            self._beam = beam_size

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self._name, device="cpu", compute_type="int8",
                download_root=os.path.join(MODELS_DIR, "whisper"),
            )
        return self._model

    def transcribe(self, audio: np.ndarray) -> str:
        audio = normalize_for_stt(audio)
        kwargs = dict(language="en", beam_size=self._beam, vad_filter=False, condition_on_previous_text=False,
                      without_timestamps=True, initial_prompt=self._prompt)
        if self._hotwords:
            kwargs["hotwords"] = self._hotwords
        with self._lock:
            model = self._load()
            try:
                segments, _info = model.transcribe(audio, **kwargs)
            except TypeError:                      # older faster-whisper without `hotwords`
                kwargs.pop("hotwords", None)
                segments, _info = model.transcribe(audio, **kwargs)
            parts = []
            for seg in segments:
                # Whisper's own convention: drop a segment only when it's BOTH
                # probably-not-speech AND low-confidence.
                if getattr(seg, "no_speech_prob", 0.0) > 0.6 and getattr(seg, "avg_logprob", 0.0) < -1.0:
                    continue
                parts.append(seg.text.strip())
        return " ".join(p for p in parts if p).strip()


class VoiceEngines:
    """Process-wide holder for the heavy models. Sessions borrow from it;
    tests substitute fakes via the constructor."""

    _shared: "VoiceEngines | None" = None
    _shared_lock = threading.Lock()

    def __init__(self, tts=None, stt=None, vad_factory: "Callable[[], object] | None" = None, wake=None, speaker=None):
        # make_tts(), not KokoroTTS(): this is what actually selects Piper when its voices are installed.
        self.tts = tts or make_tts()
        self.stt = stt or WhisperSTT()
        self.new_vad = vad_factory or SileroVAD
        self._wake = wake
        self._wake_tried = wake is not None
        self.speaker = speaker or SpeakerVerifier()

    def new_wake_stream(self):
        """A per-session acoustic wake-word stream, or None if the keyword model / sherpa-onnx isn't
        installed (everything then falls back to the transcript-only wake check, as before)."""
        if not self._wake_tried:
            self._wake_tried = True
            if kws_ready():
                try:
                    self._wake = WakeSpotter()
                except Exception as e:
                    print(f"[ember_voice] Acoustic wake word unavailable ({e}); using transcript matching only.")
        return self._wake.new_stream() if self._wake is not None else None

    @classmethod
    def shared(cls) -> "VoiceEngines":
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    def warm_up(self) -> None:
        """Loads every model and runs one throwaway inference, so the first
        real wake word doesn't pay a multi-second model-load. Call from a
        background thread at server start (run_transport.py)."""
        t0 = time.time()
        try:
            self.new_vad().prob(np.zeros(SileroVAD.FRAME, dtype=np.float32))
            self.tts.synthesize("Ready.", DEFAULT_VOICE)                     # loads the model
            t1 = time.perf_counter()
            samples, sr = self.tts.synthesize("This is a speed test of the voice engine, sir.", DEFAULT_VOICE)
            rtf = (time.perf_counter() - t1) / max(len(samples) / float(sr), 1e-6)
            name = getattr(self.tts, "name", "tts")
            note = ("" if rtf < 0.8 else
                    "  <- close to or slower than real time: expect pauses between sentences"
                    + ("; try EMBER_TTS=piper (python fetch_voice_models.py --piper)" if name != "piper" else ""))
            print(f"[ember_voice] TTS engine: {name}, real-time factor {rtf:.2f} (lower is faster; >1 means pauses){note}")
            self.stt.transcribe(np.zeros(MIC_SAMPLE_RATE, dtype=np.float32))
            wake_probe = self.new_wake_stream()
            if wake_probe is not None:
                stop_note = ("acoustic 'stop' ON — interrupts Ember immediately even mid-sentence"
                             if wake_probe.stop_keyword_active else
                             "acoustic 'stop' unavailable — falls back to the slower short-burst-transcribe path")
                print(f"[ember_voice] Wake word: acoustic 'hey ember' detector ON (plus transcript matching for "
                      f"'Ember' / 'wake up'). {stop_note}.")
            else:
                print("[ember_voice] Wake word: transcript matching only. For much more reliable 'hey ember': "
                      "pip install sherpa-onnx  and  python fetch_voice_models.py --kws")
            if self.speaker.enrolled:
                print("[ember_voice] Speaker verification: ON — only the enrolled voice can wake Ember.")
            elif speaker_verification_ready():
                print("[ember_voice] Speaker verification: model installed but no profile enrolled — "
                      "run enroll_speaker.py to lock wake to your voice. Any voice can wake Ember for now.")
            else:
                print("[ember_voice] Speaker verification: not installed — any voice can wake Ember. "
                      "See enroll_speaker.py for setup.")
            print(f"[ember_voice] Voice models warm ({time.time() - t0:.1f}s).")
        except Exception as e:
            print(f"[ember_voice] Warm-up failed: {e}")


def voice_status() -> "tuple[bool, str]":
    """(ready, human-readable summary) — what's missing for voice to work.
    Used by the transport's startup banner; never loads a model."""
    missing = []
    for module, pip_name in (("numpy", "numpy"), ("onnxruntime", "onnxruntime"), ("faster_whisper", "faster-whisper")):
        if not _module_available(module):
            missing.append(f"pip install {pip_name}")
    if not os.path.exists(SILERO_MODEL_PATH):
        missing.append("python fetch_voice_models.py")
    if TTS_ENGINE == "piper":
        engine_ok, engine = piper_ready(), "piper"
        if not engine_ok:
            missing.append("pip install piper-tts; python fetch_voice_models.py --piper")
    elif TTS_ENGINE == "kokoro":
        engine_ok, engine = kokoro_ready(), "kokoro"
        if not engine_ok:
            missing.append("pip install kokoro-onnx; python fetch_voice_models.py")
    else:
        engine = "piper" if piper_ready() else "kokoro"
        engine_ok = piper_ready() or kokoro_ready()
        if not engine_ok:
            missing.append("python fetch_voice_models.py --piper (fast voices)  or  pip install kokoro-onnx")
    if missing:
        return False, "missing: " + "; ".join(dict.fromkeys(missing))
    speaker_note = "voice-locked" if os.path.exists(SPEAKER_PROFILE_PATH) and speaker_verification_ready() else "any voice"
    return True, (f"ready (voice engine: {engine}; wake word: {'acoustic + transcript' if kws_ready() else 'transcript only'}; "
                  f"{speaker_note})")


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

_RESTORE_ADDRESS = {"status": "Ember, status"}   # ember_intent's STATUS regex anchors on the literal "Ember,"


class VoiceSession:
    """One per WebSocket connection. Thread-safe entry points, three worker
    threads (audio/VAD, utterance/STT/decision, TTS).

    Constructor callbacks — all provided by the transport:
      send(msg: dict)            thread-safe, non-blocking JSON send to THIS client
      submit_turn(text) -> bool  run process_turn(text) for this connection exactly like
                                 a typed message would be; False if a turn is in flight
      cancel_turn()              conversation.request_cancel()
      turn_active() -> bool      is a turn currently running

    Hooks the transport must call around each turn (typed OR spoken):
      begin_reply()              when a turn starts
      feed_reply(text, kind)     from the turn's stream_callback ("text" | "status")
      end_reply(final_text)      when the turn finishes (its "done")
      interrupt()                when the client sends "cancel"
    Plus feed_audio(bytes) for binary frames and handle_control(msg) for
    {"type":"voice",...} messages."""

    def __init__(
        self,
        send: Callable[[dict], None],
        submit_turn: Callable[[str], bool],
        cancel_turn: Callable[[], None],
        turn_active: Callable[[], bool],
        engines: "VoiceEngines | None" = None,
        config: "VoiceConfig | None" = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._send_raw = send
        self._submit_turn = submit_turn
        self._cancel_turn = cancel_turn
        self._turn_active = turn_active
        self.config = config or VoiceConfig()
        self._clock = clock
        engines = engines or VoiceEngines.shared()
        self._tts = engines.tts
        self._stt = engines.stt
        self._segmenter = UtteranceSegmenter(engines.new_vad(), self.config)
        self._wake = engines.new_wake_stream()          # acoustic "hey ember" detector, or None
        self._wake_hits: "collections.deque[tuple[int, str]]" = collections.deque(maxlen=32)   # (absolute sample position, keyword)
        self._wake_lock = threading.Lock()
        self._speaker = engines.speaker                 # SpeakerVerifier — fails open if unenrolled/unavailable

        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._audio_q: "queue.Queue" = queue.Queue(maxsize=300)
        self._utt_q: "queue.Queue" = queue.Queue(maxsize=4)
        self._tts_q: "queue.Queue" = queue.Queue()

        self._listening = False
        self._voice_mode = False
        self._speaker_on = False
        self._voice = DEFAULT_VOICE

        self._gen = 0
        self._seq = 0
        self._muted = False
        self._speaking_until = 0.0
        self._tts_inflight = 0
        self._awake_until = 0.0
        self._awake_after_reply = False
        self._transcribing = False
        self._ducked = False
        self._recent_spoken: "collections.deque" = collections.deque(maxlen=8)

        self._voice_turn_pending = False
        # Spoken approval of destructive actions ("allow access to X", "delete that file").
        # _spoken_confirm is switched on by the client (the HUD does, since its user is
        # talking rather than looking at the app); _confirm_pending is the one request
        # currently waiting for a yes/no.
        self._spoken_confirm = False
        self._confirm_pending: "dict | None" = None
        self._reply_voice_origin = False
        self._reply_active = False
        self._speak_reply = False
        self._chunker: "SentenceChunker | None" = None
        self._streamed_any = False
        self._status_spoken = False

        self._last_state_key = None
        self._threads: "list[threading.Thread]" = []
        self._handled = 0   # utterances fully processed — lets tests wait deterministically
        self._deaf = False
        self._rtf: "list[float]" = []
        self._rtf_warned = False
        self._debug_stream = False
        self._timing: "dict | None" = None
        self._cur_t = (0.0, 0.0)   # (utterance-end, stt-done) perf_counter stamps of the utterance being handled
        self._cur_stats = (-120.0, 0.0, 0.0)   # (RMS dBFS, peak, VAD speech ratio) of the utterance being handled
        self._cur_audio = None
        self._clip_n = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for target, name in ((self._audio_loop, "audio"), (self._utterance_loop, "utterance"), (self._tts_loop, "tts")):
            t = threading.Thread(target=target, name=f"ember-voice-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self._refresh_state(force=True)

    def stop(self) -> None:
        self._stop.set()
        for q in (self._audio_q, self._utt_q, self._tts_q):
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        for t in self._threads:
            t.join(timeout=2.0)

    # ------------------------------------------------------------------ inputs
    def feed_audio(self, pcm16: bytes) -> None:
        if not self._listening:
            return
        try:
            self._audio_q.put_nowait(pcm16)
        except queue.Full:
            try:
                self._audio_q.get_nowait()   # drop oldest rather than growing latency
            except queue.Empty:
                pass
            try:
                self._audio_q.put_nowait(pcm16)
            except queue.Full:
                pass

    def handle_control(self, msg: dict) -> None:
        action = msg.get("action")
        if action == "listen":
            self.set_listening(bool(msg.get("on")))
        elif action == "mode":
            self.set_voice_mode(bool(msg.get("on")), announce=False)
        elif action == "voice":
            name = str(msg.get("name", "")).lower()
            if name in VOICES:
                self._voice = name
                self._refresh_state(force=True)
        elif action == "speaker":
            self._speaker_on = bool(msg.get("on"))
            self._refresh_state(force=True)
        elif action == "spoken_confirm":
            self._spoken_confirm = bool(msg.get("on"))
        elif action == "barge_in":
            self.config.barge_in = bool(msg.get("on"))
        elif action == "debug":
            self._debug_stream = bool(msg.get("on"))
        elif action == "stop":
            self.interrupt()
            if self._turn_active():
                self._cancel_turn()
        elif action == "state":
            self._refresh_state(force=True)

    def set_listening(self, on: bool) -> None:
        with self._lock:
            self._listening = on
        if not on:
            self._drain(self._audio_q)
            self._segmenter.reset()
            with self._wake_lock:
                self._wake_hits.clear()
            if self._ducked:
                self._ducked = False
                self._send({"type": "voice_duck", "on": False})
        self._refresh_state(force=True)

    def set_voice_mode(self, on: bool, announce: bool = True) -> None:
        with self._lock:
            self._voice_mode = on
            if not on:
                self._awake_until = 0.0
                self._awake_after_reply = False
        self._send({"type": "voice_event", "event": "voice_mode_on" if on else "voice_mode_off"})
        self._refresh_state(force=True)
        if announce:
            self._say("Voice mode on, sir. I'm listening." if on else "Voice mode off, sir.")

    @property
    def speaker_enabled(self) -> bool:
        return self._speaker_on

    # ------------------------------------------------------------------ reply hooks
    def begin_reply(self) -> None:
        if self._is_speaking():
            self._stop_audio()
        with self._lock:
            self._muted = False
            self._reply_voice_origin = self._voice_turn_pending
            self._speak_reply = self._voice_turn_pending or self._speaker_on
            self._voice_turn_pending = False
            self._chunker = SentenceChunker() if self._speak_reply else None
            self._streamed_any = False
            self._status_spoken = False
            self._awake_after_reply = False
            self._reply_active = True
            if self._timing is not None and self._reply_voice_origin:
                self._timing["begin"] = time.perf_counter()
        self._refresh_state()

    def feed_reply(self, text: str, kind: str = "text") -> None:
        with self._lock:
            if not self._speak_reply or self._muted or self._chunker is None:
                return
            if kind == "status":
                if not self.config.speak_status or self._status_spoken or self._streamed_any:
                    return
                self._status_spoken = True
                chunks = [text]
                chunk_kind = "status"
            else:
                self._streamed_any = True
                chunks = self._chunker.feed(text)
                chunk_kind = "reply"
                if self._timing is not None and "first_text" not in self._timing:
                    self._timing["first_text"] = time.perf_counter()
        for c in chunks:
            self._enqueue_speech(c, chunk_kind)

    def end_reply(self, final_text: str = "") -> None:
        with self._lock:
            speak = self._speak_reply and not self._muted and self._chunker is not None
            chunker, streamed = self._chunker, self._streamed_any
            voice_origin = self._reply_voice_origin
            muted = self._muted
        chunks: "list[str]" = []
        if speak:
            if not streamed and final_text:
                chunks += chunker.feed(final_text)
            chunks += chunker.flush()
            if self._timing is not None and "first_text" not in self._timing:
                self._timing["first_text"] = time.perf_counter()
        for c in chunks:
            self._enqueue_speech(c, "reply")
        with self._lock:
            self._reply_active = False
            self._speak_reply = False
            self._chunker = None
            if voice_origin and not muted:
                self._awake_after_reply = True
        self._refresh_state()

    def interrupt(self) -> None:
        """Stops everything speech-related NOW: drops queued sentences,
        tells the client to flush playback, and mutes the rest of any
        in-flight reply (its remaining stream deltas are ignored)."""
        self._stop_audio()
        with self._lock:
            self._muted = True
            self._awake_after_reply = False
        self._refresh_state()

    # ------------------------------------------------------------------ internals: state
    def _is_speaking(self) -> bool:
        with self._lock:
            return self._tts_inflight > 0 or self._clock() < self._speaking_until

    def _speaker_ok(self, audio: "np.ndarray | None") -> bool:
        """True unless SpeakerVerifier gives a confident 'not the enrolled
        voice' answer — None (can't tell) and True both pass, per its own
        fail-open contract."""
        return self._speaker.matches(audio) is not False

    def _speaker_ok_continuation(self, audio: "np.ndarray | None", text: str) -> bool:
        """Speaker gate for voice-mode / follow-up utterances (no wake phrase involved).
        Not enrolled -> always True (opt-in, same as wake). Enrolled -> a confident
        mismatch is refused, AND so is a clip too short/quiet to embed ("can't tell"),
        because voice mode is permanently awake: failing open here is exactly how a
        video's short lines got through. Stop commands are exempt (cheap, recoverable,
        usually under the embedding minimum). EMBER_SPEAKER_STRICT_SHORT=0 restores
        fail-open for short clips."""
        if not self._speaker.enrolled or is_stop_command(text):
            return True
        result = self._speaker.matches(audio)
        if result is None:
            return os.environ.get("EMBER_SPEAKER_STRICT_SHORT", "1") == "0"
        return result

    def _refresh_state(self, force: bool = False) -> None:
        now = self._clock()
        turn_active = bool(self._turn_active())   # transport callback — never call it while holding our lock
        with self._lock:
            speaking = self._tts_inflight > 0 or now < self._speaking_until
            if self._awake_after_reply and not speaking and not self._reply_active and not turn_active:
                # anchor to when speech actually ENDED, not to whenever this refresh happened to run
                ended = self._speaking_until if 0 < self._speaking_until <= now else now
                self._awake_until = ended + self.config.follow_up_seconds
                self._awake_after_reply = False
            awake = self._voice_mode or now < self._awake_until
            listening, voice_mode, speaker, voice = self._listening, self._voice_mode, self._speaker_on, self._voice
            transcribing = self._transcribing
        if speaking:
            state = "speaking"
        elif turn_active:
            state = "thinking"
        elif transcribing:
            state = "transcribing"
        elif listening and self._segmenter.in_speech:
            state = "hearing"
        elif listening:
            state = "listening"
        else:
            state = "off"
        key = (state, listening, voice_mode, awake, speaker, voice)
        with self._state_lock:
            if not force and key == self._last_state_key:
                return
            self._last_state_key = key
        self._send({
            "type": "voice_state", "state": state, "listening": listening,
            "voice_mode": voice_mode, "awake": awake, "speaker": speaker, "voice": voice,
        })

    def _send(self, msg: dict) -> None:
        try:
            self._send_raw(msg)
        except Exception as e:
            print(f"[ember_voice] send failed: {e}")

    def _log(self, msg: str) -> None:
        print(f"[ember_voice] {msg}")

    def _debug(self, msg: str) -> None:
        if self.config.debug:
            print(f"[ember_voice] {msg}")

    @staticmethod
    def _drain(q: "queue.Queue") -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    # ------------------------------------------------------------------ internals: speech out
    def _stop_audio(self) -> None:
        with self._lock:
            self._gen += 1
            gen = self._gen
            while True:
                try:
                    item = self._tts_q.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    self._tts_inflight -= 1
            self._speaking_until = 0.0
        self._send({"type": "audio_stop", "gen": gen})

    def _enqueue_speech(self, text: str, kind: str = "reply") -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            gen = self._gen
            self._tts_inflight += 1
            self._recent_spoken.append((self._clock(), text))
        self._tts_q.put((gen, text, kind))

    def _say(self, text: str) -> None:
        """Speak `text` immediately, independent of any reply in flight."""
        self._enqueue_speech(text, "say")

    def _note_tts_speed(self, synth_seconds: float, audio_seconds: float) -> None:
        """If synthesis is running slower than playback, every chunk boundary is an
        audible pause. Say so once instead of leaving it as a mystery."""
        if audio_seconds < 0.5:
            return
        self._rtf.append(synth_seconds / audio_seconds)
        del self._rtf[:-8]
        if len(self._rtf) >= 3 and not self._rtf_warned:
            ordered = sorted(self._rtf)
            median = ordered[len(ordered) // 2]
            if median > 1.0:
                self._rtf_warned = True
                name = getattr(self._tts, "name", "tts")
                msg = (f"Speech synthesis ({name}) is running slower than real time (x{median:.1f}) on this machine, "
                       f"so there will be pauses between sentences."
                       + (" Switch to the faster engine: set EMBER_TTS=piper after running fetch_voice_models.py --piper." if name != "piper" else ""))
                self._log(msg)
                self._send({"type": "voice_warning", "text": msg})

    def _note_first_audio(self) -> None:
        """Emits the per-turn latency breakdown once, when the first audio of
        a voice-originated reply is sent."""
        t = self._timing
        if not t or "first_audio" in t or "first_text" not in t or "begin" not in t:
            return
        t["first_audio"] = time.perf_counter()
        self._timing = None
        endpoint = self.config.end_silence_ms / 1000.0
        stt = t["stt_done"] - t["utt_end"]
        turn_start = t["begin"] - t["submit"]
        first_text = t["first_text"] - t["begin"]
        tts = t["first_audio"] - t["first_text"]
        total = endpoint + (t["first_audio"] - t["utt_end"])
        msg = {"type": "voice_timing", "endpoint_s": round(endpoint, 2), "stt_s": round(stt, 2),
               "turn_start_s": round(turn_start, 2), "first_text_s": round(first_text, 2),
               "tts_s": round(tts, 2), "total_s": round(total, 2)}
        self._send(msg)
        self._log(f"latency (you stopped talking -> first audio): endpoint {endpoint:.2f}s + stt {stt:.2f}s + "
                  f"turn-start {turn_start:.2f}s + LLM/search-to-first-text {first_text:.2f}s + tts {tts:.2f}s = {total:.2f}s")

    def _tts_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._tts_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            gen, text, kind = item
            try:
                if gen != self._gen:
                    continue
                t_synth = time.perf_counter()
                samples, sr = self._tts.synthesize(text, self._voice)
                if samples is None or len(samples) == 0:
                    continue
                self._note_tts_speed(time.perf_counter() - t_synth, len(samples) / float(sr))
                pcm = float_to_pcm16(samples)
                with self._lock:
                    if gen != self._gen:
                        continue
                    now = self._clock()
                    self._speaking_until = max(now, self._speaking_until) + len(samples) / float(sr)
                slice_bytes = int(sr) * 2 * AUDIO_SLICE_SECONDS
                for i in range(0, len(pcm), slice_bytes):
                    with self._lock:
                        if gen != self._gen:
                            break
                        self._seq += 1
                        seq = self._seq
                    self._send({
                        "type": "audio_chunk", "gen": gen, "seq": seq, "sample_rate": sr,
                        "format": "pcm16", "data": base64.b64encode(pcm[i:i + slice_bytes]).decode("ascii"),
                    })
                    if kind == "reply" and i == 0:
                        self._note_first_audio()
            except Exception as e:
                self._log(f"TTS failed for {text[:40]!r}: {e}")
            finally:
                with self._lock:
                    self._tts_inflight -= 1
                self._refresh_state()

    # ------------------------------------------------------------------ internals: listening
    def _audio_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                self._refresh_state()
                continue
            if frame is None:
                return
            if not self._listening:
                continue
            # deaf: half-duplex, no barge-in — Ember is speaking (plus a short tail). Full
            # wake-word matching and normal command handling are still switched off here (see
            # the comment below on why), but the segmenter keeps running so a short, stop-shaped
            # burst can still reach _handle_deaf_utterance — see that method's own docstring for
            # why this is the fix for "stop"/"Ember, stop" not working while she's talking.
            deaf = False
            if not self.config.barge_in:
                deaf = self._is_deaf()
                if deaf:
                    if not self._deaf:
                        self._deaf = True
                        self._segmenter.reset()   # drop any half-heard segment; it's probably Ember herself
                elif self._deaf:
                    self._deaf = False
                    self._segmenter.reset()
            if self._wake is not None:
                # Fed even while deaf now (unlike before this fix) — see _handle_acoustic_stop's
                # docstring for why: VAD segment boundaries can't isolate a short "stop" from
                # Ember's own continuous simultaneous speech, but a streaming keyword spotter
                # doesn't need a boundary at all, it just needs the sound to occur somewhere in
                # the stream. "hey ember"/"ember"/"wake up" hits are still only ever ACTED on when
                # not deaf (see below) — only "stop" gets checked while deaf.
                hits = self._wake.accept(frame)
                if hits:
                    stop_hits = [h for h in hits if h[1] == "stop"]
                    if stop_hits:
                        self._handle_acoustic_stop()
                    if not deaf:
                        other_hits = [h for h in hits if h[1] != "stop"]
                        if other_hits:
                            with self._wake_lock:
                                self._wake_hits.extend(other_hits)
            for ev in self._segmenter.feed(frame):
                if ev.kind == "start":
                    if self.config.barge_in and self._is_speaking() and not self._ducked:
                        self._ducked = True
                        self._send({"type": "voice_duck", "on": True})
                elif ev.audio is not None:
                    if deaf:
                        self._handle_deaf_utterance(ev.audio, ev.speech_ratio)
                        continue
                    item = (ev.audio, time.perf_counter(), ev.speech_ratio, ev.start_sample)
                    try:
                        self._utt_q.put_nowait(item)
                    except queue.Full:
                        self._drain(self._utt_q)
                        self._utt_q.put_nowait(item)
                elif self.config.barge_in:
                    self._unduck()   # speech burst too short to transcribe
            self._refresh_state()

    # ------------------------------------------------------------------ spoken confirmation
    # Tools whose effect can't be undone, or that run code, are never approved by voice —
    # a stray "yes" from a video must not be able to start a script or wipe memory.
    VOICE_CONFIRM_BLOCKED = ("run_script", "clear_memory", "forget_memory")
    CONFIRM_WINDOW_S = 120.0     # same as SessionConfirmationGate.DEFAULT_TIMEOUT_SECONDS

    def can_ask_confirmation(self, tool_name: str) -> bool:
        with self._lock:
            return (self._spoken_confirm and self._listening and self._confirm_pending is None
                    and (tool_name or "").lower() not in self.VOICE_CONFIRM_BLOCKED)

    def ask_confirmation(self, request_id: str, prompt: str, resolve: Callable[[bool], None]) -> bool:
        """Speaks `prompt` and listens for yes/no — no wake word needed while it's pending.
        `resolve(approved)` is called exactly once. False if voice can't take this one
        (the on-screen dialog is still there either way)."""
        with self._lock:
            if self._confirm_pending is not None or not (self._spoken_confirm and self._listening):
                return False
            self._confirm_pending = {"id": request_id, "resolve": resolve, "until": self._clock() + self.CONFIRM_WINDOW_S, "retried": False}
        self._log(f"asking for spoken confirmation ({request_id})")
        self._say(prompt)
        return True

    def cancel_confirmation(self, request_id: str = "") -> None:
        """The request was answered some other way (a click) or went away."""
        with self._lock:
            p = self._confirm_pending
            if p is not None and (not request_id or p["id"] == request_id):
                self._confirm_pending = None

    def _answer_confirmation(self, approved: bool) -> bool:
        with self._lock:
            p, self._confirm_pending = self._confirm_pending, None
        if p is None:
            return False
        self._log(f"spoken confirmation {p['id']}: {'approved' if approved else 'denied'}")
        try:
            p["resolve"](approved)
        except Exception as e:
            self._log(f"confirmation resolve failed: {e}")
        return True

    def _try_spoken_confirmation(self, text: str, audio: "np.ndarray | None") -> bool:
        """True if this utterance was consumed as the answer (or a re-ask) to a pending
        confirmation. Only a short, clearly yes/no reply counts; a confident other-voice
        verdict from the speaker lock is refused; anything unclear asks once more, then
        waits for the on-screen buttons (and the gate's own timeout denies)."""
        with self._lock:
            p = self._confirm_pending
            if p is None:
                return False
            if self._clock() > p["until"]:
                self._confirm_pending = None
                return False
        answer = parse_confirmation_reply(text)
        if answer is None:
            return False          # ordinary speech: let the normal pipeline have it
        if not self._speaker_ok(audio):
            self._report(text, "ignored (confirmation, not enrolled voice)")
            return True
        self._report(text, f"accepted (confirmation: {answer})")
        self._answer_confirmation(answer == "yes")
        return True

    def _handle_acoustic_stop(self) -> None:
        """Fired the instant the KWS stream spots "stop" anywhere in the audio — no VAD segment,
        no transcription, no waiting for a pause. This is the actual fix for stop-while-speaking:
        _handle_deaf_utterance (below) depends on the VAD segmenter closing a short, isolated
        utterance, which it structurally cannot do while Ember's own continuous voice is also in
        the mic feed with no silence gap to hang a segment boundary on — a real "stop" said
        mid-monologue was getting buried inside one long segment that didn't close until Ember
        next paused, arriving (if at all) far too late and far too long to be recognized. A
        streaming keyword spotter has no such requirement — it flags the sound wherever it falls,
        continuously, which is exactly what's needed here.

        No-ops if there's nothing to interrupt (someone said "stop" in ordinary conversation while
        Ember was idle) so this can safely stay fed all the time, deaf or not. Deliberately NOT
        gated by speaker verification or debounced — same reasoning as _handle_deaf_utterance: a
        stray interrupt from someone else's voice is a cheap, instantly-recoverable mistake, and
        interrupt()/_cancel_turn() are already safe to call more than once if a couple of frames
        both cross the KWS threshold for the same utterance."""
        if self._confirm_pending is not None:
            self._log("stop (acoustic) while a confirmation is pending - treated as no")
            self._answer_confirmation(False)
            return
        if not (self._is_speaking() or self._turn_active()):
            return
        self._log("stop (acoustic)")
        self.interrupt()
        if self._turn_active():
            self._cancel_turn()
        self._send({"type": "voice_event", "event": "stopped"})

    def _handle_deaf_utterance(self, audio: np.ndarray, speech_ratio: float) -> None:
        """Called for a completed VAD segment heard while Ember is speaking (or in the short
        post-speech deaf tail) and barge-in is off. Full transcription/wake-matching stays off
        here on purpose — that's exactly what made "hey ember"-while-speaking unreliable without
        echo cancellation in the first place — but a short burst is still worth one quick,
        targeted look: without this, there was NO path at all for "Ember, stop" or a bare "stop"
        to reach her while she was talking, since every other route into a command is exactly
        what deaf mode exists to shut off. Longer bursts (almost certainly her own reflected
        speech) are never transcribed and never become a command, same protection as before.

        Deliberately NOT run through speaker verification (see the SpeakerVerifier section
        above) — these clips are usually well under a second, too short to embed reliably, and a
        stray "stop" from someone else's voice is a cheap, instantly-recoverable mistake next to
        a full wake letting a stranger start a conversation."""
        if not (self._is_speaking() or self._turn_active()):
            return   # nothing to interrupt — don't spend a Whisper call on it
        if len(audio) / float(MIC_SAMPLE_RATE) > self.DEAF_STOP_MAX_UTTERANCE_S:
            return   # almost certainly Ember's own voice — not worth transcribing at all
        with self._lock:
            self._transcribing = True
        self._refresh_state()
        try:
            text = (self._stt.transcribe(audio) or "").strip()
        finally:
            with self._lock:
                self._transcribing = False
        self._cur_t = (time.perf_counter(), time.perf_counter())
        self._cur_stats = (-120.0, float(np.abs(audio).max()) if len(audio) else 0.0, speech_ratio)
        self._cur_audio = audio
        if not text or is_stt_garbage(text):
            self._report(text, "dropped (empty/garbage, deaf)")
            return
        wake = detect_wake(text, self.config.aliases)
        command = wake.command if wake.matched else text
        if not is_stop_command(command):
            self._report(text, "ignored (deaf: not a stop command)")
            return
        self._report(text, "accepted (stop, deaf)")
        self._log("stop (mid-speech, half-duplex)")
        self.interrupt()
        if self._turn_active():
            self._cancel_turn()
        self._send({"type": "voice_event", "event": "stopped"})

    def _is_deaf(self) -> bool:
        with self._lock:
            return self._tts_inflight > 0 or self._clock() < self._speaking_until + self.config.deaf_tail_seconds

    def _unduck(self) -> None:
        if self._ducked:
            self._ducked = False
            self._send({"type": "voice_duck", "on": False})

    def _utterance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._utt_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            audio, t_end, ratio, start_sample = item
            try:
                self._handle_utterance(audio, t_end, ratio, start_sample)
            except Exception as e:
                self._log(f"utterance handling failed: {e}")
            finally:
                self._handled += 1
                self._unduck()
                self._refresh_state()

    def _report(self, text: str, verdict: str) -> None:
        """Console (with EMBER_VOICE_DEBUG) and/or client-side (the "debug"
        control) view of what Whisper heard and what was done about it — the
        first thing to look at when wake detection feels flaky. Includes the
        utterance's level (dBFS RMS) and VAD speech ratio: a quiet or noisy
        input shows up there long before it shows up as "she can't hear me".
        With EMBER_VOICE_SAVE_CLIPS=<dir>, the audio itself is saved too."""
        level_db, peak, ratio = self._cur_stats
        self._debug(f"{verdict}: {text!r}  (level {level_db:.0f} dBFS, peak {peak:.2f}, speech {ratio:.0%})")
        if self._debug_stream:
            self._send({"type": "voice_heard", "text": text, "verdict": verdict,
                        "stt_s": round(self._cur_t[1] - self._cur_t[0], 2),
                        "level_db": round(level_db, 1), "peak": round(peak, 3), "speech_ratio": round(ratio, 2)})
        self._save_clip(verdict)

    def _save_clip(self, verdict: str) -> None:
        d = os.environ.get("EMBER_VOICE_SAVE_CLIPS", "")
        if not d or self._cur_audio is None:
            return
        try:
            import wave
            os.makedirs(d, exist_ok=True)
            self._clip_n += 1
            slug = re.sub(r"[^a-z0-9]+", "_", verdict.lower()).strip("_")[:24]
            path = os.path.join(d, f"{time.strftime('%H%M%S')}_{self._clip_n:03d}_{slug}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(MIC_SAMPLE_RATE)
                w.writeframes(float_to_pcm16(self._cur_audio))
            clips = sorted(f for f in os.listdir(d) if f.endswith(".wav"))
            for old in clips[:-40]:               # keep the most recent 40
                os.remove(os.path.join(d, old))
        except Exception as e:
            print(f"[ember_voice] couldn't save clip: {e}")

    # After an acoustic "hey ember", audio is cut this far BEFORE the detection point before it goes to
    # Whisper. Detection lands within ~0.2 s of the end of the phrase (a little before it when speech
    # continues, a little after when it doesn't), so cutting early costs at most the tail of "ember" —
    # which strip_wake_residue() removes from the transcript — while cutting late would clip the first
    # word of the command.
    WAKE_CUT_BEFORE_S = 0.35
    # Less than this much audio after the detection point means "hey ember" and then nothing.
    BARE_WAKE_MAX_AFTER_S = 0.45
    # Longest burst worth transcribing while Ember is speaking and barge-in is off (see
    # _handle_deaf_utterance). A bare "stop"/"quiet"/"shut up"/"that's enough" said clearly fits
    # comfortably under this; anything longer during that window is almost certainly Ember's own
    # voice leaking back through an uncancelled mic, not fed to Whisper at all.
    DEAF_STOP_MAX_UTTERANCE_S = 1.8

    def _take_wake_hit(self, start: int, end: int) -> "tuple[int, str] | None":
        """The acoustic wake detection for this utterance, as (position, keyword), or None. Strong phrases
        ("hey ember") count anywhere in the utterance; soft ones ("ember", "wake up") only if they ended
        near its start. Everything up to the end of the utterance is consumed — used or stale — so one
        detection can never wake two utterances."""
        lo, hi = wake_window(start, end)
        with self._wake_lock:
            inside = [h for h in self._wake_hits if lo <= h[0] <= hi]
            self._wake_hits = collections.deque((h for h in self._wake_hits if h[0] > hi), maxlen=32)
        return pick_wake_hit(inside, start)

    def _handle_utterance(self, audio: np.ndarray, t_end: float = 0.0, speech_ratio: float = 0.0,
                          start_sample: "int | None" = None) -> None:
        hit = self._take_wake_hit(start_sample, start_sample + len(audio)) if (self._wake is not None and start_sample is not None) else None
        wake_at = hit[0] if hit else None
        acoustic = hit is not None
        acoustic_label = "acoustic" if (hit and hit[1] in KWS_STRONG) else "acoustic-soft"
        full_audio = audio
        if acoustic and hit[1] not in KWS_STRONG:
            # A soft phrase ("Ember" / "wake up") also matches "remember", "December"... Check what Whisper
            # makes of just the start of the utterance (a ~1 s clip, cheap) and veto the known look-alikes.
            head = full_audio[: max(int(0.7 * MIC_SAMPLE_RATE), wake_at - start_sample + int(0.15 * MIC_SAMPLE_RATE))]
            with self._lock:
                self._transcribing = True
            self._refresh_state()
            try:
                head_text = (self._stt.transcribe(head) or "").strip()
            finally:
                with self._lock:
                    self._transcribing = False
            if soft_wake_vetoed(head_text):
                self._cur_t = (t_end or time.perf_counter(), time.perf_counter())
                self._cur_stats = (-120.0, float(np.abs(full_audio).max()) if len(full_audio) else 0.0, speech_ratio)
                self._cur_audio = full_audio
                self._report(head_text, f"soft wake vetoed ({head_text.split()[0].lower() if head_text.split() else ''!r} is a look-alike)")
                acoustic, wake_at = False, None                    # continue on the normal transcript path
        if acoustic:
            after_s = (start_sample + len(audio) - wake_at) / float(MIC_SAMPLE_RATE)
            if after_s < self.BARE_WAKE_MAX_AFTER_S:
                # "Hey Ember" and nothing after it: no need to spend a Whisper pass at all.
                self._cur_t = (t_end or time.perf_counter(),) * 2
                self._cur_stats = (-120.0, float(np.abs(audio).max()) if len(audio) else 0.0, speech_ratio)
                self._cur_audio = full_audio
                if not self._speaker_ok(full_audio):
                    self._report("(bare wake word)", f"ignored (wake, {acoustic_label}, not enrolled voice)")
                    return
                self._report("(bare wake word)", f"accepted (wake, {acoustic_label}) - nothing after it")
                self._send({"type": "voice_event", "event": "wake"})
                self._on_bare_wake()
                return
            rel = max(0, wake_at - start_sample - int(self.WAKE_CUT_BEFORE_S * MIC_SAMPLE_RATE))
            audio = full_audio[rel:]

        with self._lock:
            self._transcribing = True
        self._refresh_state()
        try:
            text = (self._stt.transcribe(audio) or "").strip()
        finally:
            with self._lock:
                self._transcribing = False
        self._cur_t = (t_end or time.perf_counter(), time.perf_counter())
        rms = float(np.sqrt(np.mean(np.square(full_audio)))) if len(full_audio) else 0.0
        self._cur_stats = (20.0 * np.log10(max(rms, 1e-6)), float(np.abs(full_audio).max()) if len(full_audio) else 0.0, speech_ratio)
        self._cur_audio = full_audio
        if not text or is_stt_garbage(text):
            self._report(text, "dropped (empty/garbage)")
            return

        now = self._clock()
        with self._lock:
            recent = [t for ts, t in self._recent_spoken if now - ts < 60.0]
            voice_mode = self._voice_mode
            awake = voice_mode or now < self._awake_until
        if looks_like_echo(text, recent):
            self._report(text, "dropped (echo of own speech)")
            return

        if self._confirm_pending is not None and self._try_spoken_confirmation(text, full_audio):
            return

        if acoustic:
            # The sound of "hey ember" was heard, whatever Whisper made of it. What Whisper transcribed
            # is only the part after it (plus maybe a syllable of the name, which is stripped).
            command, trigger = strip_wake_residue(text, self.config.aliases), "wake"
            wake_matched = True
        else:
            wake = detect_wake(text, self.config.aliases)
            wake_matched = wake.matched
            if wake.matched:
                command, trigger = wake.command, "wake"
            elif awake:
                command, trigger = text, ("voice_mode" if voice_mode else "follow_up")
            elif (self._is_speaking() or self._turn_active()) and is_stop_command(text):
                # A bare "stop"/"quiet"/"shut up" needs no wake word and no awake/voice-mode
                # state at all when Ember is actually busy — this is the same courtesy a person
                # gets when interrupting someone talking, not an ordinary chat command that has
                # to be addressed first. is_stop_command's full-string anchor keeps this from
                # firing on an ordinary sentence that happens to contain "stop".
                command, trigger = text, "stop"
            else:
                self._report(text, "ignored (no wake phrase)")
                return

            if (wake.matched and not re.sub(r"[^A-Za-z0-9]", "", command)
                    and (speech_ratio < 0.25 or len(audio) < 0.25 * MIC_SAMPLE_RATE)):
                # A bare "Ember" out of a very short or mostly-noise clip: with the wake word
                # as a Whisper hotword, noise can occasionally be transcribed as the hotword.
                self._report(text, "dropped (bare wake word from a noisy/very short clip)")
                return

        if wake_matched and not self._speaker_ok(full_audio):
            self._report(text, f"ignored ({trigger}{', ' + acoustic_label if acoustic else ''}, not enrolled voice)")
            return
        if trigger in ("voice_mode", "follow_up") and not self._speaker_ok_continuation(full_audio, text):
            self._report(text, f"ignored ({trigger}, not enrolled voice)")
            return

        self._report(text, f"accepted ({trigger}{', ' + acoustic_label if acoustic else ''})")
        if wake_matched:
            self._send({"type": "voice_event", "event": "wake"})
        if not re.sub(r"[^A-Za-z0-9]", "", command):
            self._on_bare_wake()
            return
        self._accept_command(command.strip(), trigger)

    def _on_bare_wake(self) -> None:
        if self._is_speaking() or self._turn_active():
            self.interrupt()
            if self._turn_active():
                self._cancel_turn()
        with self._lock:
            self._awake_until = self._clock() + self.config.wake_window_seconds
        self._log("wake (bare) -> acknowledging")
        self._say(self.config.ack_phrase)
        self._refresh_state(force=True)

    def _accept_command(self, command: str, trigger: str) -> None:
        vm = parse_voice_mode_command(command)
        if vm is not None:
            self._log(f"voice mode {vm} ({trigger})")
            self.set_voice_mode(vm == "on", announce=True)
            if vm == "off":
                with self._lock:
                    self._awake_until = 0.0
            return

        busy = self._is_speaking() or self._turn_active()
        if not busy and is_stop_command(command):
            # Nothing running: don't send "stop" to the LLM as a chat turn (she'd answer "Nothing's
            # actively running..."). Still flush the client, whose audio queue can outlast the
            # server's estimate of when she stopped talking.
            self._log(f"stop ({trigger}) while idle - ignored, flushing client audio")
            self.interrupt()
            self._send({"type": "voice_event", "event": "stopped"})
            return
        if busy and is_stop_command(command):
            self._log(f"stop ({trigger})")
            self.interrupt()
            if self._turn_active():
                self._cancel_turn()
            self._send({"type": "voice_event", "event": "stopped"})
            return

        if busy:
            self.interrupt()
            if self._turn_active():
                self._answer_confirmation(False)   # a turn parked on an approval can't unwind otherwise
                self._cancel_turn()
                if not self._wait_turn_idle(self.config.busy_wait_seconds):
                    self._log("barge-in: previous turn didn't unwind in time")
                    self._say("Still finishing the last one, sir. Say that again in a moment.")
                    return

        self._log(f"command ({trigger}): {command!r}")
        self._send({"type": "transcript", "text": command, "trigger": trigger})
        with self._lock:
            self._voice_turn_pending = True
            self._timing = {"utt_end": self._cur_t[0], "stt_done": self._cur_t[1], "submit": time.perf_counter()}
        ok = False
        try:
            ok = bool(self._submit_turn(_RESTORE_ADDRESS.get(re.sub(r"[^a-z]", "", command.lower()), command)))
        except Exception as e:
            self._log(f"submit_turn failed: {e}")
        if not ok:
            with self._lock:
                self._voice_turn_pending = False

    def _wait_turn_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._turn_active():
                return True
            time.sleep(0.05)
        return not self._turn_active()
