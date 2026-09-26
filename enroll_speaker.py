"""
enroll_speaker.py
===================
One-time (or repeatable) speaker enrollment for Ember's voice-lock feature
— "only my voice should activate it" (see ember_voice.py's SpeakerVerifier
and the "Speaker verification" section of its module docstring for the
full design and its fail-open contract).

Records a few short samples of YOUR voice directly from the default
microphone — independent of the WebSocket transport; this is an offline
setup step you run once from a terminal, not something that happens while
Ember is live — and saves their voiceprints to
data/voice_models/speaker_profile.json.

Once that file exists (and the speaker-verification model is installed),
ember_voice.py refuses to treat a wake phrase ("hey Ember" / bare
"Ember"/"wake up" / a trailing "...Ember") as a genuine activation unless
the speaker's voiceprint matches one of the enrolled samples above
EMBER_SPEAKER_THRESHOLD (default 0.5, cosine similarity — a first guess,
not tuned; lower it if your own voice is being rejected, raise it if a
housemate is still waking her). This does NOT lock the "stop"/"quiet"/
"shut up" interrupt, and does NOT lock continuations inside an
already-open voice-mode/follow-up window — see ember_voice.py's docstring
for why.

Before this script has ever been run successfully, or if the
speaker-verification model isn't installed, wake acceptance is completely
unchanged — this is opt-in by having a profile, not a separate flag that
could be left half-configured.

Usage:
    python enroll_speaker.py                 # 5 samples, ~3s each (default)
    python enroll_speaker.py --samples 8 --seconds 4
    python enroll_speaker.py --reset         # wipe the existing profile first, then re-enroll

Requires: sounddevice (pip install sounddevice), sherpa-onnx, and the
speaker-verification model file at the path SpeakerVerifier expects
(EMBER_SPEAKER_MODEL, default data/voice_models/
nemo_en_speakerverification_speakernet.onnx — download it from the
sherpa-onnx model zoo if fetch_voice_models.py in your checkout doesn't
already fetch it).
"""

import argparse
import os
import sys

import numpy as np

from ember_voice import (
    MIC_SAMPLE_RATE, SPEAKER_MODEL_PATH, SPEAKER_PROFILE_PATH,
    SpeakerVerifier, speaker_verification_ready,
)


def _record(seconds: float, sample_rate: int = MIC_SAMPLE_RATE) -> np.ndarray:
    import sounddevice as sd
    audio = sd.rec(int(seconds * sample_rate), samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    return audio.reshape(-1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=int, default=5, help="How many clips to record (default 5).")
    parser.add_argument("--seconds", type=float, default=3.0, help="Length of each clip in seconds (default 3.0).")
    parser.add_argument("--reset", action="store_true", help="Wipe the existing profile before enrolling.")
    args = parser.parse_args()

    if not speaker_verification_ready():
        print(f"Speaker-verification model not found at {SPEAKER_MODEL_PATH}.")
        print("Install it (nemo_en_speakerverification_speakernet.onnx from the sherpa-onnx model zoo, "
              "saved to that path) and `pip install sherpa-onnx sounddevice` if you haven't already, "
              "then re-run this script.")
        sys.exit(1)

    try:
        import sounddevice  # noqa: F401  -- just checking it's importable before recording anything
    except ImportError:
        print("sounddevice isn't installed — `pip install sounddevice` and re-run this script.")
        sys.exit(1)

    if args.reset and os.path.exists(SPEAKER_PROFILE_PATH):
        os.remove(SPEAKER_PROFILE_PATH)
        print(f"Removed existing profile at {SPEAKER_PROFILE_PATH}.")

    verifier = SpeakerVerifier()
    enrolled = 0
    print(f"Recording {args.samples} sample(s), {args.seconds:.1f}s each.")
    print("Speak naturally for the full clip — a real sentence or two, not just \"hey Ember\" — e.g.:")
    print('  "Hey Ember, what\'s on my calendar today? Also remind me to call the dentist tomorrow."\n')

    for i in range(args.samples):
        input(f"[{i + 1}/{args.samples}] Press Enter, then start talking immediately...")
        audio = _record(args.seconds)
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if peak < 0.02:
            print("  That came through very quiet or silent — skipped. Check your mic and try again.\n")
            continue
        if verifier.enroll(audio):
            enrolled += 1
            print(f"  Captured (peak level {peak:.2f}).\n")
        else:
            print("  Couldn't extract a usable voiceprint from that clip — skipped.\n")

    if enrolled == 0:
        print("No samples were enrolled — the profile is unchanged. Voice-lock stays off until this succeeds.")
        sys.exit(1)

    print(f"Done — {enrolled} sample(s) saved to {SPEAKER_PROFILE_PATH}.")
    print("Restart Ember's transport server for the new profile to take effect.")
    print("If your own voice ever gets rejected, run this again to add a few more samples "
          "(each run adds to the profile rather than replacing it, unless you pass --reset), "
          "or lower EMBER_SPEAKER_THRESHOLD a little.")


if __name__ == "__main__":
    main()
