"""Settings speaking-speed multiplier: control message, clamping, and the TTS engines honouring it."""
import os, sys, types
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ember_voice as ev


class FakeKokoro:
    def __init__(self): self.calls = []
    def create(self, text, voice, speed, lang):
        self.calls.append(speed)
        return np.zeros(100, dtype=np.float32), 24000


def _kokoro_engine():
    t = ev.KokoroTTS.__new__(ev.KokoroTTS)
    t._kokoro = FakeKokoro(); t._cache = {}
    import threading; t._lock = threading.Lock()
    t._load = lambda: t._kokoro
    return t


def test_kokoro_multiplies_voice_preset_by_user_speed():
    t = _kokoro_engine()
    t.synthesize("hello", "daniel")                    # default
    t.synthesize("hello", "daniel", speed=1.25)        # different speed must NOT hit the 1.0 cache entry
    assert t._kokoro.calls == [ev.VOICES["daniel"].speed, ev.VOICES["daniel"].speed * 1.25]


def test_piper_uses_smaller_length_scale_when_faster():
    import threading
    t = ev.PiperTTS.__new__(ev.PiperTTS)
    t._cache = {}; t._voices = {}; t._lock = threading.Lock()
    seen = {}

    class V:
        config = types.SimpleNamespace(length_scale=1.0, sample_rate=22050)
        def synthesize(self, text, syn_config=None):
            seen["scale"] = None if syn_config is None else syn_config.length_scale
            return [types.SimpleNamespace(audio_float_array=np.zeros(50, dtype=np.float32))]

    t._resolve = lambda k: "x"; t._voices["x"] = V()
    t.synthesize("hello there", "daniel")
    assert seen["scale"] is None
    t.synthesize("hello there", "daniel", speed=1.25)
    assert abs(seen["scale"] - 0.8) < 1e-6
    t.synthesize("hello there", "daniel", speed=0.8)
    assert abs(seen["scale"] - 1.25) < 1e-6


def _session_stub():
    s = ev.VoiceSession.__new__(ev.VoiceSession)
    s._speech_speed = 1.0; s._voice = "daniel"
    s._refresh_state = lambda force=False: None
    return s


def test_speed_control_is_clamped_and_ignores_junk():
    s = _session_stub()
    s.handle_control({"action": "speed", "value": 1.3});   assert s._speech_speed == 1.3
    s.handle_control({"action": "speed", "value": 99});    assert s._speech_speed == ev.MAX_SPEECH_SPEED
    s.handle_control({"action": "speed", "value": 0.01});  assert s._speech_speed == ev.MIN_SPEECH_SPEED
    s.handle_control({"action": "speed", "value": "fast"}); assert s._speech_speed == ev.MIN_SPEECH_SPEED
    s.handle_control({"action": "speed", "value": float("nan")}); assert s._speech_speed == ev.MIN_SPEECH_SPEED
    s.handle_control({"action": "speed"});                 assert s._speech_speed == ev.MIN_SPEECH_SPEED
