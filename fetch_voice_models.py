"""
fetch_voice_models.py
======================
One-time download of the voice models into data/voice_models/.

    python fetch_voice_models.py                  # everything
    python fetch_voice_models.py --piper --kws    # recommended: fast voices + the acoustic "hey ember" detector
    python fetch_voice_models.py --kokoro         # Kokoro voices (slower, a bit nicer sounding)

Groups (Silero VAD is always fetched):  --piper  --kokoro  --kws   (none given = all three)

  silero_vad.onnx                    ~2 MB    voice-activity detection (needed by both)
  piper/en_GB-alan-medium.onnx       ~63 MB   "daniel"   (British male)   + .onnx.json
  piper/en_GB-jenny_dioco-medium     ~63 MB   "isabella" (British female) + .onnx.json
  piper/en_US-amy-medium             ~63 MB   "heart"    (US female)      + .onnx.json
  kokoro-v1.0.onnx / voices-v1.0.bin ~325 MB  Kokoro TTS (fp32) and its voice styles
  kws/*                              ~14 MB   acoustic wake-word model (sherpa-onnx streaming keyword spotter);
                                              needs:  pip install sherpa-onnx

Kokoro is fp32 on purpose: the int8 build produced garbage audio under some
onnxruntime versions, and ember_voice.py refuses to play audio like that.
Piper voices come from Hugging Face (rhasspy/piper-voices); if a download fails
you can also run:  python -m piper.download_voices en_GB-alan-medium --download-dir data/voice_models/piper

The faster-whisper speech-to-text model downloads itself on first use.
"""

import argparse
import os
import sys
import urllib.error
import urllib.request

DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "voice_models")

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
SILERO = [("silero_vad.onnx", "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx", 1_000_000)]
KOKORO = [
    ("kokoro-v1.0.onnx", "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx", 300_000_000),
    ("voices-v1.0.bin", "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", 20_000_000),
]
PIPER = []
for stem, path in (
    ("en_GB-alan-medium", "en/en_GB/alan/medium"),
    ("en_GB-jenny_dioco-medium", "en/en_GB/jenny_dioco/medium"),
    ("en_US-amy-medium", "en/en_US/amy/medium"),
):
    PIPER.append((f"piper/{stem}.onnx", f"{HF}/{path}/{stem}.onnx", 20_000_000))
    PIPER.append((f"piper/{stem}.onnx.json", f"{HF}/{path}/{stem}.onnx.json", 1_000))


KWS_ARCHIVE = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
               "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2")
KWS_NEEDED = [
    "tokens.txt",
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
]


def _fetch_kws() -> bool:
    """Downloads the keyword-spotting archive and keeps only the four files ember_voice.py uses,
    flat in data/voice_models/kws/."""
    import tarfile
    import tempfile
    out_dir = os.path.join(DEST, "kws")
    os.makedirs(out_dir, exist_ok=True)
    if all(os.path.exists(os.path.join(out_dir, f)) for f in KWS_NEEDED):
        print("  kws/: already present")
        return True
    print("  kws/: downloading the wake-word model (~18 MB)...")
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "kws.tar.bz2")
        try:
            urllib.request.urlretrieve(KWS_ARCHIVE, archive)
            with tarfile.open(archive, "r:bz2") as tar:
                for member in tar.getmembers():
                    base = os.path.basename(member.name)
                    if member.isfile() and base in KWS_NEEDED:
                        with tar.extractfile(member) as src, open(os.path.join(out_dir, base), "wb") as dst:
                            dst.write(src.read())
        except (urllib.error.URLError, OSError, tarfile.TarError) as e:
            print(f"    FAILED: {e}\n    ({KWS_ARCHIVE})")
            return False
    missing = [f for f in KWS_NEEDED if not os.path.exists(os.path.join(out_dir, f))]
    if missing:
        print(f"    FAILED: archive didn't contain {missing}")
        return False
    return True


def _download(name: str, url: str, min_bytes: int) -> bool:
    path = os.path.join(DEST, *name.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) >= min_bytes:
        print(f"  {name}: already present")
        return True
    tmp = path + ".part"
    print(f"  {name}: downloading...")

    def hook(blocks, block_size, total):
        if total > 0:
            sys.stdout.write(f"\r    {min(100, blocks * block_size * 100 // total):3d}%")
            sys.stdout.flush()

    try:
        urllib.request.urlretrieve(url, tmp, hook)
    except (urllib.error.URLError, OSError) as e:
        print(f"\n    FAILED: {e}\n    ({url})")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    print()
    if os.path.getsize(tmp) < min_bytes:
        os.remove(tmp)
        print(f"    FAILED: {name} downloaded but is suspiciously small")
        return False
    os.replace(tmp, path)
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--piper", action="store_true", help="Piper voices")
    ap.add_argument("--kokoro", action="store_true", help="Kokoro voices")
    ap.add_argument("--kws", action="store_true", help="acoustic 'hey ember' wake-word model")
    args = ap.parse_args()
    everything = not (args.piper or args.kokoro or args.kws)
    files = list(SILERO)
    if args.piper or everything:
        files += PIPER
    if args.kokoro or everything:
        files += KOKORO
    print(f"Voice models -> {DEST}")
    failures = [name for name, url, size in files if not _download(name, url, size)]
    if (args.kws or everything) and not _fetch_kws():
        failures.append("kws/")
    if failures:
        raise SystemExit(f"\n{len(failures)} download(s) failed: {', '.join(failures)}")
    print("\nDone. Needed packages: pip install piper-tts faster-whisper sherpa-onnx   (and kokoro-onnx if you want Kokoro)")
