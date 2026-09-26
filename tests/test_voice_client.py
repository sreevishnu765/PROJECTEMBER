import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from voice_client import AutoGain, Player, apply_gain, find_token, level_stats, pick_device, read_dotenv_token  # noqa: E402


def tone(amp, n=640, freq=0.05):
    return (amp * 32767 * np.sin(np.arange(n) * freq)).astype("<i2").tobytes()


class LevelTests(unittest.TestCase):
    def test_levels(self):
        db, peak = level_stats(tone(0.5))
        self.assertAlmostEqual(peak, 0.5, delta=0.02)
        self.assertAlmostEqual(db, -9.0, delta=0.6)          # RMS of a 0.5 sine is 0.354 -> -9 dBFS
        db, peak = level_stats(b"\x00\x00" * 640)
        self.assertLess(db, -100)
        self.assertEqual(peak, 0.0)

    def test_gain_clips_instead_of_wrapping(self):
        out = np.frombuffer(apply_gain(tone(0.5), 10.0), dtype="<i2")
        self.assertEqual(int(out.max()), 32767)
        self.assertEqual(int(out.min()), -32768)

    def test_auto_gain_lifts_quiet_speech_but_not_the_noise_floor(self):
        agc = AutoGain()
        for _ in range(80):
            out = agc.process(tone(0.01))
        self.assertGreater(level_stats(out)[1], 0.05)         # 0.01 peak lifted several-fold
        self.assertLessEqual(agc.gain, 12.0)
        quiet = AutoGain()
        for _ in range(80):
            quiet.process(tone(0.002))                        # below the noise floor: left alone
        self.assertEqual(quiet.gain, 1.0)

    def test_auto_gain_never_clips(self):
        agc = AutoGain()
        for _ in range(50):
            agc.process(tone(0.02))
        out = agc.process(tone(0.9))
        self.assertLessEqual(level_stats(out)[1], 0.99)


class PlayerTests(unittest.TestCase):
    def test_saves_at_the_servers_sample_rate_and_drops_stale_chunks(self):
        import tempfile
        import wave
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.wav")
            pl = Player(save_path=path)
            pl.push(tone(0.3, 2205), gen=1, src_rate=22050)          # Piper-style 22.05 kHz
            pl.flush(gen=2)                                          # interrupt: anything older is stale
            pl.push(tone(0.3, 2205), gen=1, src_rate=22050)          # late stale chunk -> ignored
            pl.push(tone(0.3, 2205), gen=2, src_rate=22050)
            pl.close()
            with wave.open(path) as w:
                self.assertEqual(w.getframerate(), 22050)
                self.assertEqual(w.getnframes(), 4410)

    def test_resamples_to_the_output_rate(self):
        pl = Player(save_path=None)
        pl._rate = 48000
        pl.push(tone(0.3, 2205), gen=0, src_rate=22050)
        self.assertAlmostEqual(len(pl._buf) / 2, 2205 * 48000 / 22050, delta=2)


class TokenLookupTests(unittest.TestCase):
    def test_dotenv_parsing_and_precedence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w") as f:
                f.write('# c\nOTHER=1\nEMBER_TRANSPORT_TOKEN="abc123"\n')
            self.assertEqual(read_dotenv_token(path), "abc123")
            self.assertIsNone(read_dotenv_token(os.path.join(tmp, "missing.env")))
        self.assertEqual(find_token("cli-wins"), "cli-wins")
        with unittest.mock.patch.dict(os.environ, {"EMBER_TRANSPORT_TOKEN": "env-token"}):
            self.assertEqual(find_token(None), "env-token")


class ConnectionRefusedTests(unittest.TestCase):
    def test_no_server_gives_an_explanation_not_a_traceback(self):
        import socket
        import subprocess
        import tempfile
        with socket.socket() as sock:
            sock.bind(("localhost", 0))
            port = sock.getsockname()[1]              # a port nothing is listening on
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "voice_client.py"),
                 "--url", f"ws://localhost:{port}", "--token", "x", "--no-mic",
                 "--save-audio", os.path.join(tmp, "o.wav"), "--wait", "0"],
                capture_output=True, text=True, timeout=30,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("python run_transport.py", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class DevicePickTests(unittest.TestCase):
    devices = [
        {"name": "Microphone Array (Realtek)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Speakers (Realtek)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Microphone Array (Realtek)", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Headset Mic (Realtek)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
    ]

    def test_by_index_and_name(self):
        self.assertEqual(pick_device("3", self.devices, "input"), 3)
        self.assertEqual(pick_device("headset", self.devices, "input"), 3)
        self.assertEqual(pick_device("speakers", self.devices, "output"), 1)
        self.assertIsNone(pick_device(None, self.devices, "input"))

    def test_prefers_default_host_api(self):
        self.assertEqual(pick_device("array", self.devices, "input", preferred_hostapi=2), 2)
        self.assertEqual(pick_device("array", self.devices, "input", preferred_hostapi=0), 0)

    def test_errors(self):
        with self.assertRaises(SystemExit):
            pick_device("nonexistent", self.devices, "input")
        with self.assertRaises(SystemExit):
            pick_device("1", self.devices, "input")            # index 1 is output-only
        with self.assertRaises(SystemExit):
            pick_device("speakers", self.devices, "input")


if __name__ == "__main__":
    unittest.main()
