"""
voice_tune.py
==============
Measures and tunes Ember's acoustic wake word on YOUR voice and YOUR microphone.

Why: the wake-word model was trained on other people's speech. Whether "Hey Ember" is caught on the first
try depends on your accent, your mic and your room — and the right sensitivity is different for everyone.
This script records you reading a short script (about 3 minutes), runs the detector over the recordings at
a grid of sensitivity settings, and picks the MOST sensitive setting that still produces no false wakes on
the phrases you read (the look-alikes: "remember to buy milk", "the amber light", "December", ...).

    python voice_tune.py                        # record, evaluate, print recommended settings
    python voice_tune.py --apply                # ...and save them to .env
    python voice_tune.py --mic "Microphone Array"
    python voice_tune.py --from-dir data\\tune_clips   # re-evaluate recordings you already made
    python voice_tune.py --list-devices

Recordings are kept in data/tune_clips/<group>/NN.wav (16 kHz mono), so you can re-run the evaluation
after changing anything, or send the folder to whoever is helping you tune it.

Groups:  hey = "Hey Ember ..."   ember = "Ember ..." (no "hey")   wake = "Wake up ..."   neg = things you
read that must NOT wake her.
"""

import argparse
import glob
import os
import sys
import time
import wave

import numpy as np

from ember_voice import (KWS_DIR, KWS_SOFT_MAX_FROM_START_S, KWS_STRONG, MIC_SAMPLE_RATE, SILERO_MODEL_PATH, SileroVAD,
                         UtteranceSegmenter, VoiceConfig, WakeSpotter, float_to_pcm16, kws_ready, pick_wake_hit,
                         wake_window)
from ember_voice_text import SOFT_VETO_WORDS, detect_wake

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLIP_DIR = os.path.join(ROOT, "data", "tune_clips")

# What you'll be asked to say. Say them the way you actually would, from where you actually sit.
PROMPTS = (
    [("hey", t) for t in ("Hey Ember", "Hey Ember, what's the time?", "Hey Ember, what's on my calendar tomorrow?", "Hey Ember, voice mode on")] * 2
    + [("ember", t) for t in ("Ember, what's the weather like?", "Ember, which is the next race weekend?", "Ember", "Ember, remind me to call mom")] * 2
    + [("wake", t) for t in ("Wake up", "Wake up, Ember")] * 2
    + [("neg", t) for t in (
        "Remember to buy milk", "December is cold this year", "The amber light was on", "I need to embed this file",
        "What time is it", "Hey there, how are you", "Member when we went to the lake", "Wake me up at seven tomorrow",
        "The members of the team arrived", "Amber alert issued in three states", "September twenty sixth is the race",
        "Who is the current champion", "I'll be there in November", "Can you help me with my homework",
        "Hey, are you coming to dinner", "The embers glowed in the fireplace")]
)

NEG_TEXTS = [t for g, t in PROMPTS if g == "neg"]
# Negatives whose first word the transcript veto would reject ("Remember to buy milk"): a soft false alarm on
# these is harmless in production, so it isn't held against the soft setting. Only applied when a folder
# holds exactly the recordings this script's prompts produce.
VETOABLE_NEG = {i for i, t in enumerate(NEG_TEXTS) if t.split()[0].lower() in SOFT_VETO_WORDS}

GRID_SCORES = (1.0, 1.5, 2.0, 3.0)
GRID_THRESHOLDS = (0.25, 0.20, 0.15, 0.10)
CHUNK_BYTES = 1280            # 40 ms of int16 audio = what the client sends


# ------------------------------------------------------------------ audio helpers
def normalize_like_client(audio: np.ndarray, target_rms: float = 0.06, max_gain: float = 12.0) -> np.ndarray:
    """Approximates the client's automatic mic gain, so tuning sees what the server will see."""
    x = np.asarray(audio, dtype=np.float32)
    if x.size < 640:
        return x
    blocks = x[: len(x) // 640 * 640].reshape(-1, 640)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    voiced = rms[rms > 0.004]
    if voiced.size == 0:
        return x
    gain = min(max_gain, target_rms / float(np.median(voiced)))
    # Limit by a robust peak (99.5th percentile), not the absolute maximum: one start-of-recording pop must not
    # cap the boost for the whole clip (the live client's gain recovers after a transient, too). Anything that
    # still exceeds full scale after the boost is clipped, exactly as it would be on the wire.
    peak = float(np.percentile(np.abs(x), 99.5))
    if peak * gain > 0.98:
        gain = 0.98 / peak
    return np.clip(x * max(1.0, gain), -1.0, 1.0).astype(np.float32)


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: need 16-bit PCM")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != MIC_SAMPLE_RATE:
        x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * MIC_SAMPLE_RATE / rate)), np.arange(len(x)), x).astype(np.float32)
    return x


def write_wav(path: str, audio: np.ndarray) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_SAMPLE_RATE)
        w.writeframes(float_to_pcm16(audio))


def load_dir(directory: str) -> "dict[str, list[np.ndarray]]":
    clips = {}
    for group in ("hey", "ember", "wake", "neg"):
        files = sorted(glob.glob(os.path.join(directory, group, "*.wav")))
        clips[group] = [read_wav(f) for f in files]
    return clips


# ------------------------------------------------------------------ recording
def record_session(mic_device=None, seconds: float = 3.6, directory: str = DEFAULT_CLIP_DIR) -> "dict[str, list[np.ndarray]]":
    import sounddevice as sd
    info = sd.query_devices(mic_device, "input")
    print(f"\nMicrophone: {info['name']}\nYou'll be shown {len(PROMPTS)} phrases. For each: wait for GO, then say it once,\n"
          f"normally, from where you usually sit. {seconds:.1f} s is recorded each time. Ctrl+C stops early.\n")
    input("Press Enter to begin... ")
    clips = {"hey": [], "ember": [], "wake": [], "neg": []}
    counts = {g: 0 for g in clips}
    for i, (group, text) in enumerate(PROMPTS, 1):
        label = {"hey": "wake phrase", "ember": "wake phrase", "wake": "wake phrase", "neg": "just read it (must NOT wake her)"}[group]
        print(f"\n[{i}/{len(PROMPTS)}] {label}:   \"{text}\"")
        time.sleep(0.9)
        print("   GO", flush=True)
        rec = sd.rec(int(seconds * MIC_SAMPLE_RATE), samplerate=MIC_SAMPLE_RATE, channels=1, dtype="float32", device=mic_device)
        sd.wait()
        audio = rec[:, 0].copy()
        counts[group] += 1
        write_wav(os.path.join(directory, group, f"{counts[group]:02d}.wav"), audio)
        clips[group].append(audio)
    return clips


# ------------------------------------------------------------------ evaluation
class Prepared:
    """A recording as the server would receive it, plus what the speech detector found and basic audio stats."""

    def __init__(self, pcm, segments, level_db, peak, clipped_pct):
        self.pcm, self.segments = pcm, segments
        self.level_db, self.peak, self.clipped_pct = level_db, peak, clipped_pct

    def __iter__(self):                         # (pcm, segments) unpacking, as before
        return iter((self.pcm, self.segments))

    def describe(self) -> str:
        segs = ", ".join(f"{a / MIC_SAMPLE_RATE:.1f}-{b / MIC_SAMPLE_RATE:.1f}s" for a, b in self.segments) or "none"
        return (f"level {self.level_db:.0f} dBFS, peak {self.peak:.2f}, clipped {self.clipped_pct:.1f}%, "
                f"speech segments: {segs}")

    def segment_audio(self) -> np.ndarray:
        """float32 audio of the first utterance (what would go to Whisper), or the whole clip if none was found."""
        x = np.frombuffer(self.pcm, dtype="<i2").astype(np.float32) / 32768.0
        if self.segments:
            a, b = self.segments[0]
            return x[a:b]
        return x


def audio_stats(audio: np.ndarray) -> "tuple[float, float, float]":
    """(level of the loudest 20% of 32 ms blocks in dBFS, absolute peak, % of samples at full scale after the
    client-style boost) for a raw recording."""
    x = np.asarray(audio, dtype=np.float32)
    n = len(x) // 512
    if n == 0:
        return -120.0, 0.0, 0.0
    rms = np.sqrt(np.mean(x[: n * 512].reshape(n, 512) ** 2, axis=1))
    loud = np.sort(rms)[-max(1, n // 5):]
    boosted = normalize_like_client(x)
    return (20.0 * float(np.log10(max(float(np.mean(loud)), 1e-6))), float(np.abs(x).max()),
            100.0 * float(np.mean(np.abs(boosted) >= 0.999)))


def prepare_clip(audio: np.ndarray, vad_model_path: str = SILERO_MODEL_PATH):
    """A Prepared record (pcm bytes, segments, stats) for one recording: the audio as the server would receive it (client-style gain,
    0.5 s of lead-in and 1 s of tail silence) and the utterance segments the REAL segmenter (Silero VAD) finds
    in it, as absolute (start, end) sample positions. Segmentation doesn't depend on the wake-word settings,
    so this is done once per clip, not once per setting."""
    padded = np.concatenate([np.zeros(int(0.5 * MIC_SAMPLE_RATE), np.float32), normalize_like_client(audio),
                             np.zeros(MIC_SAMPLE_RATE, np.float32)])
    pcm = float_to_pcm16(padded)
    segmenter = UtteranceSegmenter(SileroVAD(vad_model_path), VoiceConfig(debug=False))
    segments = []
    for i in range(0, len(pcm), CHUNK_BYTES):
        for ev in segmenter.feed(pcm[i:i + CHUNK_BYTES]):
            if ev.kind == "end" and ev.audio is not None:
                segments.append((ev.start_sample, ev.start_sample + len(ev.audio)))
    return Prepared(pcm, segments, *audio_stats(audio))


def detect_hits(spotter: WakeSpotter, pcm: bytes) -> "list[tuple[int, str]]":
    """[(absolute_sample_position, keyword), ...] using exactly the production code path."""
    stream = spotter.new_stream()
    hits = []
    for i in range(0, len(pcm), CHUNK_BYTES):
        hits += stream.accept(pcm[i:i + CHUNK_BYTES])
    return hits


def wake_result(hits, segments) -> "tuple[tuple[int, str] | None, str]":
    """What the live system would do with this clip: (the detection that would wake her or None, why not).
    Applies the same attribution window and selection rule as VoiceSession."""
    for start, end in segments:
        lo, hi = wake_window(start, end)
        hit = pick_wake_hit([h for h in hits if lo <= h[0] <= hi], start)
        if hit is not None:
            return hit, "woke"
    if not hits:
        return None, "the detector heard nothing that sounded like a wake phrase"
    if not segments:
        return None, "the speech detector found no utterance in the clip"
    pos, name = min(hits)
    late = (pos - segments[0][0]) / MIC_SAMPLE_RATE
    return None, (f"heard {name!r}, but {late:.1f}s after you started speaking — bare 'Ember'/'Wake up' must be the "
                  f"FIRST thing said (limit {KWS_SOFT_MAX_FROM_START_S}s)")


def clip_outcome(group: str, hit) -> "tuple[bool, bool, bool]":
    """(found, strong_false_wake, soft_false_wake) for one clip given the wake decision `hit`."""
    if group == "neg":
        return False, bool(hit and hit[1] in KWS_STRONG), bool(hit and hit[1] not in KWS_STRONG)
    return hit is not None, False, False


def evaluate_grid(clips, scores=GRID_SCORES, thresholds=GRID_THRESHOLDS, model_dir: str = KWS_DIR,
                  vad_model_path: str = SILERO_MODEL_PATH, progress=None, prepared=None):
    """{(score, thr): {"hey": (found, total), "ember": ..., "wake": ..., "neg_strong": (false, total),
    "neg_soft": (false, total)}}"""
    import tempfile
    prepared = prepared or {g: [prepare_clip(a, vad_model_path) for a in arrays] for g, arrays in clips.items()}
    results = {}
    use_veto_list = len(clips.get("neg", [])) == len(NEG_TEXTS)
    with tempfile.TemporaryDirectory() as tmp:
        for score in scores:
            for thr in thresholds:
                spotter = WakeSpotter(model_dir=model_dir, threshold=thr, score=score, soft_threshold=thr, soft_score=score,
                                      soft=True, keywords_path=os.path.join(tmp, "kw.txt"))
                row = {}
                for group, items in prepared.items():
                    found = strong_fw = soft_fw = 0
                    for i, item in enumerate(items):
                        pcm, segments = item
                        hit, _ = wake_result(detect_hits(spotter, pcm), segments)
                        ok, sfw, ofw = clip_outcome(group, hit)
                        found += ok
                        strong_fw += sfw
                        soft_fw += ofw and not (use_veto_list and group == "neg" and i in VETOABLE_NEG)
                    if group == "neg":
                        row["neg_strong"], row["neg_soft"] = (strong_fw, len(items)), (soft_fw, len(items))
                    else:
                        row[group] = (found, len(items))
                results[(score, thr)] = row
                if progress:
                    progress(score, thr, row)
    return results, prepared


def diagnose(prepared, cfg, model_dir: str = KWS_DIR, limit: int = 4) -> "list[str]":
    """Plain-language reasons for the misses (and false wakes) at one setting — so a low number is explained."""
    import tempfile
    lines = []
    with tempfile.TemporaryDirectory() as tmp:
        spotter = WakeSpotter(model_dir=model_dir, threshold=cfg[1], score=cfg[0], soft_threshold=cfg[1], soft_score=cfg[0],
                              soft=True, keywords_path=os.path.join(tmp, "kw.txt"))
        for group in ("hey", "ember", "wake", "neg"):
            shown = 0
            for i, item in enumerate(prepared.get(group, []), 1):
                hit, why = wake_result(detect_hits(spotter, item.pcm), item.segments)
                if group == "neg":
                    if hit is not None and shown < limit:
                        lines.append(f"  neg/{i:02d}.wav: WOKE on {hit[1]!r} (should not have)")
                        shown += 1
                elif hit is None and shown < limit:
                    lines.append(f"  {group}/{i:02d}.wav: missed — {why}\n        [{item.describe()}]")
                    shown += 1
    return lines


# ------------------------------------------------------------------ Whisper (transcript) evaluation
def stt_grid(models, hotwords=("Ember", ""), beams=(1, 5)):
    return [{"model": m, "hotwords": h, "beam": b} for m in models for h in hotwords for b in beams]


def evaluate_stt(prepared, configs, make_stt=None, progress=None):
    """Transcribes every recording with each Whisper configuration and applies the SAME transcript wake check
    the server uses (ember_voice_text.detect_wake). Returns, per configuration:
      {"found": {"hey": (n, total), "ember": ..., "wake": ...}, "false": (n, total), "avg_s": seconds per clip,
       "text": {(group, index): transcript}, "woke": {(group, index): bool}}"""
    import time as _time
    if make_stt is None:
        from ember_voice import WhisperSTT
        engines = {}

        def make_stt(cfg):
            stt = engines.setdefault(cfg["model"], WhisperSTT(model_name=cfg["model"]))
            stt.set_options(hotwords=cfg["hotwords"], beam_size=cfg["beam"])
            return stt
    out = {}
    for cfg in configs:
        stt = make_stt(cfg)
        found, text, woke = {}, {}, {}
        false_wakes = total_neg = 0
        elapsed = clips_done = 0.0
        for group, items in prepared.items():
            n_ok = 0
            for i, item in enumerate(items, 1):
                t0 = _time.perf_counter()
                heard = (stt.transcribe(item.segment_audio()) or "").strip()
                elapsed += _time.perf_counter() - t0
                clips_done += 1
                matched = detect_wake(heard).matched
                text[(group, i)], woke[(group, i)] = heard, matched
                if group == "neg":
                    total_neg += 1
                    false_wakes += matched
                else:
                    n_ok += matched
            if group != "neg":
                found[group] = (n_ok, len(items))
        key = (cfg["model"], cfg["hotwords"], cfg["beam"])
        out[key] = {"found": found, "false": (false_wakes, total_neg), "avg_s": elapsed / max(1.0, clips_done),
                    "text": text, "woke": woke}
        if progress:
            progress(key, out[key])
    return out


def recommend_stt(results: dict) -> tuple:
    """Most wakes found with at most one false wake; ties go to the faster configuration."""
    def found(r):
        return sum(n for n, _ in r["found"].values())
    ok = {k: r for k, r in results.items() if r["false"][0] <= 1} or results
    top = max(found(r) for r in ok.values())
    return min((k for k, r in ok.items() if found(r) == top), key=lambda k: results[k]["avg_s"])


def print_stt_report(results: dict, best: tuple, acoustic: "dict | None" = None) -> None:
    print("\nWhisper (transcript) wake check on your recordings — found / total, per setting:")
    print(f"  {'model':<12}{'hotword':<9}{'beam':>4}   {'hey':>6} {'ember':>6} {'wake':>6}   {'false':>7}   {'s/clip':>6}")
    for key, r in sorted(results.items(), key=lambda kv: (kv[0][0], kv[0][1] == "", kv[0][2])):
        model, hot, beam = key
        f = r["found"]
        print(f"  {model:<12}{(hot or '(off)'):<9}{beam:>4}   {f['hey'][0]:>2}/{f['hey'][1]:<3} {f['ember'][0]:>2}/{f['ember'][1]:<3} "
              f"{f['wake'][0]:>2}/{f['wake'][1]:<3}   {r['false'][0]:>3}/{r['false'][1]:<3}   {r['avg_s']:>6.2f}"
              + ("   <- best" if key == best else ""))
    r = results[best]
    if acoustic is not None:
        both = {g: sum(1 for i in range(1, total + 1) if acoustic.get((g, i)) or r["woke"].get((g, i)))
                for g, (_, total) in r["found"].items()}
        totals = {g: t for g, (_, t) in r["found"].items()}
        print(f"\nCoverage with the best Whisper setting PLUS the sound detector (either one wakes her): "
              f"hey {both['hey']}/{totals['hey']}, ember {both['ember']}/{totals['ember']}, wake {both['wake']}/{totals['wake']}")
    print("\nWhat Whisper wrote for the ones it missed (best setting):")
    shown = 0
    for (group, i), heard in sorted(r["text"].items()):
        if group != "neg" and not r["woke"][(group, i)] and shown < 12:
            print(f"  {group}/{i:02d}.wav -> {heard!r}")
            shown += 1
    if shown == 0:
        print("  (none — it caught them all)")
    model, hot, beam = best
    print("\nRecommended Whisper settings:")
    print(f"  EMBER_STT_MODEL={model}\n  EMBER_STT_HOTWORDS={hot}\n  EMBER_STT_BEAM={beam}")


def stt_env_lines(best: tuple) -> "dict[str, str]":
    return {"EMBER_STT_MODEL": best[0], "EMBER_STT_HOTWORDS": best[1], "EMBER_STT_BEAM": str(best[2])}


def recommend(results: dict) -> dict:
    """"Hey Ember": the most sensitive setting with NO false wakes from that phrase. Bare "Ember"/"Wake up":
    the most sensitive setting with at most one soft false wake. Ties go to the LESS sensitive setting
    (fewer surprises in the wild)."""
    def sensitivity(cfg):            # bigger = more sensitive
        score, thr = cfg
        return (score, -thr)

    def best(group_keys, neg_key, max_false):
        ok = [(cfg, row) for cfg, row in results.items() if row[neg_key][0] <= max_false]
        pool = ok or sorted(results.items(), key=lambda kv: (kv[1][neg_key][0], sensitivity(kv[0])))[:1]

        def found(row):
            return sum(row[g][0] for g in group_keys)
        top = max(found(row) for _, row in pool)
        winners = [cfg for cfg, row in pool if found(row) == top]
        return min(winners, key=sensitivity), bool(ok)

    hey_cfg, hey_ok = best(("hey",), "neg_strong", 0)
    soft_cfg, soft_ok = best(("ember", "wake"), "neg_soft", 1)
    return {"hey": hey_cfg, "soft": soft_cfg, "hey_clean": hey_ok, "soft_clean": soft_ok}


def env_lines(rec: dict) -> "dict[str, str]":
    return {
        "EMBER_KWS_SCORE": f"{rec['hey'][0]}", "EMBER_KWS_THRESHOLD": f"{rec['hey'][1]}",
        "EMBER_KWS_SOFT_SCORE": f"{rec['soft'][0]}", "EMBER_KWS_SOFT_THRESHOLD": f"{rec['soft'][1]}",
    }


def upsert_env(path: str, values: "dict[str, str]") -> None:
    """Sets KEY=VALUE lines in a .env file: replaces an existing KEY line, otherwise appends. Nothing else is touched."""
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def print_report(results: dict, rec: dict) -> None:
    print("\nsensitivity sweep (found / total).  FALSE WAKES = things you read that must NOT wake her:")
    print(f"  {'score':>5} {'thr':>5}   {'hey':>7} {'ember':>7} {'wake':>7}   {'false: hey':>10} {'false: soft':>11}")
    for (score, thr), row in sorted(results.items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        mark = "  <- hey pick" if (score, thr) == rec["hey"] else ("  <- soft pick" if (score, thr) == rec["soft"] else "")
        print(f"  {score:5.1f} {thr:5.2f}   {row['hey'][0]:>3}/{row['hey'][1]:<3} {row['ember'][0]:>3}/{row['ember'][1]:<3} "
              f"{row['wake'][0]:>3}/{row['wake'][1]:<3}   {row['neg_strong'][0]:>4}/{row['neg_strong'][1]:<5} {row['neg_soft'][0]:>5}/{row['neg_soft'][1]:<5}{mark}")
    h, s = results[rec["hey"]], results[rec["soft"]]
    print(f"\nRecommended for \"Hey Ember\": score {rec['hey'][0]}, threshold {rec['hey'][1]} -> found {h['hey'][0]}/{h['hey'][1]}, "
          f"false wakes {h['neg_strong'][0]}/{h['neg_strong'][1]}")
    print(f"Recommended for bare \"Ember\" / \"Wake up\": score {rec['soft'][0]}, threshold {rec['soft'][1]} -> found "
          f"{s['ember'][0] + s['wake'][0]}/{s['ember'][1] + s['wake'][1]}, false wakes {s['neg_soft'][0]}/{s['neg_soft'][1]} "
          f"(look-alike words like 'remember' are caught by a separate transcript check and aren't counted)")
    if not rec["hey_clean"]:
        print("\nNOTE: every setting woke her on something you read for \"Hey Ember\"; the least-bad one was chosen. "
              "Re-record in a quieter spot, or raise the threshold by hand (EMBER_KWS_THRESHOLD).")
    if not rec["soft_clean"]:
        print("\nNOTE: the bare \"Ember\"/\"Wake up\" phrases false-woke more than once at every setting; the least-bad "
              "was chosen. EMBER_KWS_SOFT=0 turns them off (\"Hey Ember\" would remain).")
    print("\nAdd to .env (or run again with --apply):")
    for k, v in env_lines(rec).items():
        print(f"  {k}={v}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-dir", help="evaluate existing recordings instead of recording (folders hey/ ember/ wake/ neg/)")
    ap.add_argument("--dir", default=DEFAULT_CLIP_DIR, help="where new recordings are saved")
    ap.add_argument("--mic", default=None, help="input device: index or part of its name")
    ap.add_argument("--apply", action="store_true", help="save the recommended settings to .env")
    ap.add_argument("--stt", nargs="*", default=None, metavar="MODEL",
                    help="ALSO compare Whisper settings on the recordings (e.g. --stt base.en small.en); with no model "
                         "names, tests the current one. Downloads any model it hasn't got yet.")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args(argv)

    if not kws_ready():
        print("The wake-word model isn't installed: pip install sherpa-onnx  and  python fetch_voice_models.py --kws")
        return 1
    if args.list_devices:
        from voice_client import print_devices
        print_devices()
        return 0

    if args.from_dir:
        clips = load_dir(args.from_dir)
    else:
        from voice_client import pick_device, require_sounddevice
        sd = require_sounddevice()
        device = pick_device(args.mic, sd.query_devices(), "input", sd.default.hostapi)
        try:
            clips = record_session(device, directory=args.dir)
        except KeyboardInterrupt:
            print("\nStopped early — evaluating what was recorded.")
            clips = load_dir(args.dir)
    if not clips["neg"] or not (clips["hey"] or clips["ember"]):
        print("Not enough recordings to tune (need some 'hey'/'ember' and some 'neg' clips).")
        return 1
    if not os.path.exists(SILERO_MODEL_PATH):
        print(f"The speech-detection model is missing ({SILERO_MODEL_PATH}) — run: python fetch_voice_models.py --piper --kws")
        return 1
    print(f"\nEvaluating {sum(len(v) for v in clips.values())} clips over {len(GRID_SCORES) * len(GRID_THRESHOLDS)} settings...")
    results, prepared = evaluate_grid(clips)
    rec = recommend(results)
    print_report(results, rec)
    notes = diagnose(prepared, rec["hey"]) + [l for l in diagnose(prepared, rec["soft"]) if "missed" in l and not l.strip().startswith("hey/")]
    if notes:
        print("\nWhy the misses (at the recommended setting):")
        for l in dict.fromkeys(notes):
            print(l)
    env_to_write = dict(env_lines(rec))
    if args.stt is not None:
        models = args.stt or [os.environ.get("EMBER_STT_MODEL", "base.en")]
        configs = stt_grid(models)
        print(f"\nTranscribing with {len(configs)} Whisper setting(s) x {sum(len(v) for v in prepared.values())} clips "
              f"(first use of a model downloads it)...")
        try:
            stt_results = evaluate_stt(prepared, configs,
                                       progress=lambda key, r: print(f"  done: {key[0]} hotword={key[1] or 'off'} beam={key[2]}", flush=True))
        except ImportError:
            print("faster-whisper isn't installed: pip install faster-whisper")
            return 1
        best_stt = recommend_stt(stt_results)
        acoustic = {}
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sp = WakeSpotter(model_dir=KWS_DIR, threshold=rec["hey"][1], score=rec["hey"][0], soft_threshold=rec["soft"][1],
                             soft_score=rec["soft"][0], soft=True, keywords_path=os.path.join(tmp, "kw.txt"))
            for group, items in prepared.items():
                for i, item in enumerate(items, 1):
                    acoustic[(group, i)] = wake_result(detect_hits(sp, item.pcm), item.segments)[0] is not None
        print_stt_report(stt_results, best_stt, acoustic)
        env_to_write.update(stt_env_lines(best_stt))
    if args.apply:
        env_path = os.path.join(ROOT, ".env")
        upsert_env(env_path, env_to_write)
        print(f"\nSaved to {env_path}. Restart the server for it to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
