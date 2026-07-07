/**
 * Neural voice-activity detection via the Silero VAD model (same ONNX model the
 * Pipecat framework uses server-side), running in an AudioWorklet through
 * @ricky0123/vad-web.
 *
 * Why: the RMS energy VAD in audio.ts cannot tell speech from a cough, keyboard
 * clatter, or street noise — every loud burst fires a barge-in candidate that
 * halts bot audio and burns an STT round trip. Silero classifies *speech*
 * specifically, so barge-in only triggers on actual talking.
 *
 * This module is lazy-loaded (dynamic import) so the ~2MB model + WASM runtime
 * never touch the initial bundle, and MicCapture falls back to the RMS path if
 * anything here fails (asset 404, CSP-blocked WASM, ancient browser).
 */

// Matches MicCapture's semantics so swapping engines doesn't change call feel:
// redemption == silenceMs (trailing silence before end-of-utterance) and
// minSpeech == minSpeechMs (ignore sub-length blips).
export interface SileroVadOptions {
  stream: MediaStream;
  audioContext: AudioContext;
  onSpeechStart: () => void;
  /** pcm16 is the utterance as 16kHz mono Float32 samples straight from the
   *  worklet — the semantic end-of-turn gate consumes it server-side, sparing
   *  the backend any audio decoding. */
  onSpeechEnd: (pcm16: Float32Array) => void;
  onMisfire: () => void;
  redemptionMs?: number;
  minSpeechMs?: number;
  positiveSpeechThreshold?: number;
  negativeSpeechThreshold?: number;
}

interface MicVadLike {
  destroy: () => Promise<void> | void;
}

export class SileroVadEngine {
  private constructor(private readonly vad: MicVadLike) {}

  /**
   * Loads the vad-web runtime and starts listening on the given stream.
   * Throws on any load/init failure — the caller keeps the RMS fallback.
   */
  static async create(opts: SileroVadOptions): Promise<SileroVadEngine> {
    const { MicVAD } = await import('@ricky0123/vad-web');
    const vad = await MicVAD.new({
      model: 'v5',
      baseAssetPath: '/',
      onnxWASMBasePath: '/',
      // Share MicCapture's stream and context: MicVAD must never own (or stop)
      // the mic — MediaRecorder records from the same tracks. The default
      // pauseStream implementation calls track.stop(), which would kill the
      // recorder mid-call, so both hooks are neutralised.
      audioContext: opts.audioContext,
      getStream: async () => opts.stream,
      pauseStream: async () => {},
      resumeStream: async () => opts.stream,
      startOnLoad: true,
      positiveSpeechThreshold: opts.positiveSpeechThreshold ?? 0.5,
      negativeSpeechThreshold: opts.negativeSpeechThreshold ?? 0.35,
      redemptionMs: opts.redemptionMs ?? 500,
      minSpeechMs: opts.minSpeechMs ?? 300,
      // The segment audio feeds the end-of-turn gate, not the recording
      // (MediaRecorder stays the recording source of truth) — no padding needed.
      preSpeechPadMs: 0,
      onSpeechStart: opts.onSpeechStart,
      onSpeechEnd: (audio: Float32Array) => opts.onSpeechEnd(audio),
      onVADMisfire: opts.onMisfire,
    });
    return new SileroVadEngine(vad);
  }

  destroy(): void {
    try {
      void this.vad.destroy();
    } catch {
      /* already destroyed */
    }
  }
}
