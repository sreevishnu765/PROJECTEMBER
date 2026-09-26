"""
voice_bench.py
===============
Measures, on THIS machine, the three numbers that decide how voice feels:

  1. TTS speed  — for each installed engine (Piper, Kokoro), seconds-of-compute
     per second-of-audio (the "real-time factor"). Below 1.0 the next chunk is ready before the current one has
     finished playing, so speech is gapless once it starts. Above 1.0 you'll
     hear pauses between chunks.
  2. The chunk plan — how a typical reply gets cut up (ember_voice_text.py's
     latency ramp), with the predicted time to first audio and any predicted gaps.
  3. STT — per Whisper model: load time, transcription time, and what it
     writes for synthesized "Hey Ember ..." phrases plus whether the wake-word
     matcher accepts it. (Synthetic speech isn't your voice or your mic, so
     treat recognition results as a rough guide to name-spelling problems,
     not as an accuracy score.)

Usage:
    python voice_bench.py                      # TTS + chunk plan + STT with base.en
    python voice_bench.py --stt tiny.en base.en small.en
    python voice_bench.py --no-stt
"""

import argparse
import os
import time

import numpy as np

from ember_voice import (DEFAULT_VOICE, KokoroTTS, PiperTTS, WhisperSTT, VoiceModelsMissing,
                         kokoro_ready, piper_ready)
from ember_voice_text import SentenceChunker, detect_wake

SAMPLE_REPLY = (
    "It's an Austrian energy drink brand founded in 1987, born from a modified Thai formula "
    "called Krating Daeng, and it sold nearly 14 billion cans globally last year, sir."
)
WAKE_PHRASES = [
    "Hey Ember, what time is it?",
    "Ember, what's on my calendar tomorrow?",
    "Hey Ember.",
    "Wake up.",
    "Ember, remind me to call mom at five pm.",
    "What can you tell me about Red Bull, Ember?",
]


def to_16k(samples: np.ndarray, sr: int) -> np.ndarray:
    n = int(len(samples) * 16000 / sr)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


def bench_tts(tts) -> None:
    print(f"\n== TTS ({tts.name}, voice '{DEFAULT_VOICE}', {os.cpu_count()} CPU threads visible) ==")
    tts.synthesize("Warm up.", DEFAULT_VOICE)   # first call pays model load
    whole_dt = 0.0
    for text in ("Good evening, sir.", SAMPLE_REPLY[:52], SAMPLE_REPLY[:100], SAMPLE_REPLY):
        t0 = time.perf_counter()
        samples, sr = tts.synthesize(text, DEFAULT_VOICE)
        dt = whole_dt = time.perf_counter() - t0
        audio = len(samples) / sr
        print(f"  {len(text):4d} chars -> {audio:5.1f}s audio in {dt:4.1f}s   real-time factor {dt / audio:4.2f}")

    print("\n== Chunk plan for a typical one-sentence reply ==")
    ch = SentenceChunker()
    chunks = ch.feed(SAMPLE_REPLY) + ch.flush()
    t_ready, playing_until, first_audio = 0.0, 0.0, None
    for i, c in enumerate(chunks, 1):
        t0 = time.perf_counter()
        samples, sr = tts.synthesize(c, DEFAULT_VOICE)
        synth = time.perf_counter() - t0
        t_ready += synth                      # synthesis is sequential on one worker
        start = max(t_ready, playing_until)
        gap = max(0.0, t_ready - playing_until) if i > 1 else 0.0
        if first_audio is None:
            first_audio = t_ready
        playing_until = start + len(samples) / sr
        print(f"  chunk {i}: {len(c):3d} chars  synth {synth:3.1f}s  audio {len(samples) / sr:3.1f}s  "
              f"{'GAP %.1fs before it' % gap if gap > 0.15 else 'gapless'}")
    print(f"  -> first audio {first_audio:.1f}s after the reply text is available "
          f"(synthesizing the whole reply as one piece took {whole_dt:.1f}s)")


def bench_stt(models: "list[str]", tts) -> None:
    clips = {}
    for voice in ("daniel", "heart"):
        for phrase in WAKE_PHRASES:
            samples, sr = tts.synthesize(phrase, voice)
            pad = np.zeros(8000, dtype=np.float32)
            clips[(voice, phrase)] = np.concatenate([pad, to_16k(samples, sr), pad])

    for name in models:
        print(f"\n== STT: faster-whisper {name} (int8, CPU) ==")
        stt = WhisperSTT(model_name=name)
        try:
            t0 = time.perf_counter()
            stt.transcribe(np.zeros(16000, dtype=np.float32))
            print(f"  load + first call: {time.perf_counter() - t0:.1f}s")
        except Exception as e:
            print(f"  couldn't load {name}: {e}")
            continue
        ok = total = 0
        times = []
        for (voice, phrase), audio in clips.items():
            t0 = time.perf_counter()
            text = stt.transcribe(audio)
            dt = time.perf_counter() - t0
            times.append(dt)
            wake = detect_wake(text)
            total += 1
            ok += wake.matched
            print(f"  [{voice:6s}] {dt:4.1f}s  {text!r:52s} -> {'WAKE, command=%r' % wake.command if wake.matched else 'no wake'}")
        print(f"  wake recognized on {ok}/{total} synthetic clips; median transcription {np.median(times):.1f}s "
              f"for ~{np.mean([len(a) / 16000 for a in clips.values()]):.1f}s clips")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stt", nargs="*", default=["base.en"], help="Whisper models to test, e.g. tiny.en base.en small.en")
    ap.add_argument("--no-stt", action="store_true")
    args = ap.parse_args()
    engines = []
    if piper_ready():
        engines.append(PiperTTS())
    if kokoro_ready():
        engines.append(KokoroTTS())
    if not engines:
        raise SystemExit("No TTS engine is set up — run: python fetch_voice_models.py --piper  (and pip install piper-tts)")
    for tts in engines:
        try:
            bench_tts(tts)
        except VoiceModelsMissing as e:
            print(f"  {e}")
    print("\nReading the numbers: real-time factor below ~0.7 is comfortable; near or above 1.0 means audible pauses "
          "between sentences. ember_voice picks Piper automatically when it's installed (EMBER_TTS=kokoro to override).")
    if not args.no_stt:
        bench_stt(args.stt, engines[0])
