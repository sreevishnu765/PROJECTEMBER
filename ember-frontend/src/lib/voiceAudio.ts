// src/lib/voiceAudio.ts
//
// Low-level Web Audio plumbing for Ember's voice pipeline — mic capture
// (resampled + PCM16-encoded to match ember_voice.py's expected wire
// format: 16 kHz mono, signed 16-bit little-endian, sent as raw binary
// WebSocket frames — see voice_client.py's send_mic()) and streaming TTS
// playback (base64 PCM16 chunks at whatever rate the server synthesized
// at, queued back-to-back with generation-based flush/duck support,
// mirroring voice_client.py's Player class).
//
// Deliberately uses ScriptProcessorNode rather than an AudioWorkletNode
// for capture: it's deprecated but universally supported in every
// Chromium version Electron ships, and an AudioWorklet needs a separately
// loadable module file whose URL resolution differs between Vite's dev
// server (http://localhost:5173) and Electron's packaged build
// (file://.../dist/index.html) — not worth that risk for a first pass.
// Nothing else here depends on the choice; swapping to an AudioWorklet
// later only touches startMicCapture.

export const MIC_SAMPLE_RATE = 16000;
const SEND_FRAME_SAMPLES = 640; // 40 ms @ 16 kHz — matches voice_client.py's BLOCK; not required to match, just consistent

export type MicCapture = {
  stop: () => void;
};

/**
 * Starts capturing the microphone, resampled to 16 kHz mono, and calls
 * `onFrame` with a 640-sample Int16 PCM ArrayBuffer roughly every 40ms —
 * ready to send directly as a WebSocket binary frame (ws.send(frame) sends
 * exactly the raw bytes ember_transport.py's `isinstance(raw_msg, bytes)`
 * branch expects, no base64/JSON wrapping needed for outgoing audio).
 *
 * Requests real echo cancellation/noise suppression/auto-gain from the
 * browser — this is the actual, real fix for the half-duplex workarounds
 * that were built for the raw Python client (which has no AEC at all):
 * Chromium's getUserMedia constraints do proper acoustic echo cancellation
 * against whatever this SAME page is currently playing back, so the
 * server-side deaf/acoustic-stop-keyword machinery becomes a safety net
 * here rather than the only line of defense — real barge-in is safe to
 * enable (see useEmberChat's startVoiceListening, which does).
 */
export async function startMicCapture(onFrame: (frame: ArrayBuffer) => void): Promise<MicCapture> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });

  // Some browsers only approximate the requested context sample rate (or
  // ignore it outright on certain platforms/devices) — resampling below is
  // done by hand regardless of what was actually granted, so this works
  // either way rather than assuming the request was honored.
  let audioContext: AudioContext;
  try {
    audioContext = new AudioContext({ sampleRate: MIC_SAMPLE_RATE });
  } catch {
    audioContext = new AudioContext();
  }

  const source = audioContext.createMediaStreamSource(stream);
  const processor = audioContext.createScriptProcessor(2048, 1, 1);
  let carry = new Float32Array(0);
  const ratio = audioContext.sampleRate / MIC_SAMPLE_RATE;

  processor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    // Cheap linear-interpolation resample to 16 kHz. This is speech-
    // recognition input, not high-fidelity audio — the server's own
    // Whisper pass dominates any quality difference this could introduce,
    // same reasoning voice_client.py's own WAV-loading resample uses.
    const resampled =
      ratio === 1
        ? input
        : (() => {
            const outLen = Math.max(1, Math.round(input.length / ratio));
            const out = new Float32Array(outLen);
            for (let i = 0; i < outLen; i++) {
              const srcPos = i * ratio;
              const i0 = Math.floor(srcPos);
              const i1 = Math.min(i0 + 1, input.length - 1);
              const frac = srcPos - i0;
              out[i] = input[i0] * (1 - frac) + input[i1] * frac;
            }
            return out;
          })();

    const combined = new Float32Array(carry.length + resampled.length);
    combined.set(carry, 0);
    combined.set(resampled, carry.length);

    let offset = 0;
    while (combined.length - offset >= SEND_FRAME_SAMPLES) {
      const slice = combined.subarray(offset, offset + SEND_FRAME_SAMPLES);
      const pcm16 = new Int16Array(SEND_FRAME_SAMPLES);
      for (let i = 0; i < SEND_FRAME_SAMPLES; i++) {
        const s = Math.max(-1, Math.min(1, slice[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      onFrame(pcm16.buffer);
      offset += SEND_FRAME_SAMPLES;
    }
    carry = combined.subarray(offset);
  };

  source.connect(processor);
  // ScriptProcessorNode only fires onaudioprocess while connected through
  // to a live destination in every browser — routed through a silent gain
  // node so none of this is actually audible.
  const silence = audioContext.createGain();
  silence.gain.value = 0;
  processor.connect(silence);
  silence.connect(audioContext.destination);

  return {
    stop: () => {
      processor.onaudioprocess = null;
      processor.disconnect();
      source.disconnect();
      silence.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      audioContext.close().catch(() => {});
    },
  };
}

/**
 * Streaming playback queue for base64 PCM16 chunks from the server,
 * mirroring voice_client.py's Player class: each chunk carries a
 * generation number, a stale chunk (gen < the last flush's gen) is
 * dropped rather than played, flush() cuts off whatever's queued/playing
 * immediately (a genuine barge-in/stop, not just "stop accepting new
 * audio"), and ducking is a simple gain multiplier during barge-in.
 */
export class VoicePlaybackQueue {
  private audioContext: AudioContext;
  private gainNode: GainNode;
  private nextStartTime = 0;
  private minGen = 0;
  private scheduled: AudioBufferSourceNode[] = [];

  constructor() {
    this.audioContext = new AudioContext();
    this.gainNode = this.audioContext.createGain();
    this.gainNode.connect(this.audioContext.destination);
  }

  /** Browsers suspend a freshly created AudioContext until a user gesture
   * — call this from the same click handler that starts voice listening. */
  resume(): void {
    if (this.audioContext.state === "suspended") {
      this.audioContext.resume().catch(() => {});
    }
  }

  push(base64Pcm16: string, gen: number, sampleRate: number): void {
    if (gen < this.minGen) return; // late chunk from before a flush/interrupt — see Player.push's own min_gen check
    let bytes: Uint8Array;
    try {
      const binary = atob(base64Pcm16);
      bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    } catch {
      return; // malformed chunk — drop rather than throw and kill the whole session
    }
    const pcm16 = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.length / 2));
    if (pcm16.length === 0) return;
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) float32[i] = pcm16[i] / 32768;

    const buffer = this.audioContext.createBuffer(1, float32.length, sampleRate);
    buffer.copyToChannel(float32, 0);

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gainNode);

    const now = this.audioContext.currentTime;
    const startAt = Math.max(now, this.nextStartTime);
    source.start(startAt);
    this.nextStartTime = startAt + buffer.duration;
    this.scheduled.push(source);
    source.onended = () => {
      this.scheduled = this.scheduled.filter((s) => s !== source);
    };
  }

  /** Cuts off whatever's playing/queued right now — a real interrupt. */
  flush(gen: number): void {
    this.minGen = Math.max(this.minGen, gen);
    this.nextStartTime = this.audioContext.currentTime;
    for (const source of this.scheduled) {
      try {
        source.stop();
      } catch {
        // already finished naturally between the filter check and this call — fine
      }
    }
    this.scheduled = [];
  }

  setDucked(ducked: boolean): void {
    this.gainNode.gain.setTargetAtTime(ducked ? 0.25 : 1.0, this.audioContext.currentTime, 0.05);
  }

  close(): void {
    this.flush(Number.MAX_SAFE_INTEGER);
    this.audioContext.close().catch(() => {});
  }
}
