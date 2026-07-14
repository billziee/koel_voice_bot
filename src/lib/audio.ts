// Real microphone capture with VAD endpointing.
//
// Runs a MediaRecorder continuously and detects speech→silence boundaries.
// Each detected utterance is stopped, encoded (webm/opus where supported),
// base64'd, and handed to `onUtterance` for the live call to forward to STT
// (Groq Whisper). Leading silence is harmless — Whisper trims it — and
// re-arming happens in `onstop` so segments never overlap.
//
// Two detection engines, one state machine:
//   1. Silero neural VAD (src/lib/vad.ts) — primary. Classifies speech
//      specifically, so coughs/keyboard/traffic don't fire barge-in.
//      Loaded asynchronously after start(); capture begins on RMS.
//   2. RMS energy + adaptive noise floor — fallback (and the onLevel meter
//      source). Takes over automatically if the neural engine fails to load.

import { SileroVadEngine } from './vad';

// pcm16B64 (s16le mono 16kHz, from the Silero worklet) rides along when the
// neural engine endpointed the utterance — it feeds the backend's semantic
// end-of-turn gate. Absent on the RMS fallback path.
export type UtteranceHandler = (audioB64: string, format: string, pcm16B64?: string) => void;

export interface MicCaptureOptions {
  onUtterance: UtteranceHandler;
  onSpeechStart?: () => void; // fires the instant speech begins (for barge-in)
  onLevel?: (rms: number) => void; // 0..1, for the waveform / mic meter
  // Streaming (ws) mode only: fires continuously with raw PCM16 (base64, s16le
  // mono at `sampleRate`) as audio is captured, for incremental server-side STT.
  // Only wired when provided — the default per-utterance path is unaffected.
  onFrame?: (pcm16B64: string, sampleRate: number) => void;
  onError?: (err: unknown) => void;
  thresholdRms?: number; // absolute floor for speech vs. silence — never go below this
  noiseMultiplier?: number; // speech must clear (ambient noise floor * this) too
  noiseFloorAlpha?: number; // EMA smoothing for the rolling ambient noise estimate
  silenceMs?: number; // trailing silence that ends an utterance
  minSpeechMs?: number; // ignore sub-threshold blips (coughs, clicks)
  maxIdleMs?: number; // recycle the recorder if no speech, to bound memory
}

interface Codec {
  mime: string;
  format: string;
}

function pickCodec(): Codec | null {
  const candidates: Codec[] = [
    { mime: 'audio/webm;codecs=opus', format: 'webm' },
    { mime: 'audio/webm', format: 'webm' },
    { mime: 'audio/ogg;codecs=opus', format: 'ogg' },
    { mime: 'audio/mp4', format: 'mp4' },
  ];
  const supported = (m: string) =>
    typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported?.(m);
  return candidates.find((c) => supported(c.mime)) ?? null;
}

async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  return bytesToBase64(bytes);
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function float32ToPcm16Base64(samples: Float32Array): string {
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  return bytesToBase64(new Uint8Array(pcm.buffer));
}

export function micSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof MediaRecorder !== 'undefined' &&
    pickCodec() !== null
  );
}

export class MicCapture {
  private readonly opts: Required<Omit<MicCaptureOptions, 'onError'>> & Pick<MicCaptureOptions, 'onError'>;
  private codec: Codec | null = null;
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private recorder: MediaRecorder | null = null;
  private chunks: BlobPart[] = [];
  private timer: ReturnType<typeof setInterval> | null = null;
  private stopped = true;
  // Continuous-frame tap (streaming/ws mode only). A ScriptProcessorNode taps
  // every sample (no gaps); a zero-gain sink drives it without playing the mic
  // back out. Only built when a frame sink (onFrame) was supplied.
  private readonly hasFrameSink: boolean;
  private frameTap: ScriptProcessorNode | null = null;
  private frameTapSink: GainNode | null = null;

  // VAD state
  private hadSpeech = false;
  private lastVoiceAt = 0;
  private speechStartAt = 0;
  private segmentStartAt = 0;
  // Rolling ambient noise estimate (RMS), adapted only during non-speech ticks.
  // A single fixed threshold either misses quiet speech or false-triggers on
  // loud background chatter (public places) — this makes the cutoff track the
  // room instead of assuming one fixed noise level everywhere.
  private noiseFloor = 0;
  // Which engine currently owns speech start/end decisions. RMS from t=0 so the
  // call is never deaf while the neural model loads; flips to 'silero' when
  // (and only if) the model comes up.
  private engine: 'rms' | 'silero' = 'rms';
  private silero: SileroVadEngine | null = null;
  // Utterance PCM from the Silero worklet, held between speech-end and the
  // recorder's onstop emit (they fire in that order within the same segment).
  private pendingPcm16B64: string | undefined;

  constructor(options: MicCaptureOptions) {
    // Capture whether a real frame sink was supplied *before* defaults fill one
    // in, so chunk mode never builds the streaming tap.
    this.hasFrameSink = typeof options.onFrame === 'function';
    this.opts = {
      thresholdRms: 0.025,
      noiseMultiplier: 2.5,
      noiseFloorAlpha: 0.05,
      // Trailing silence before we call end-of-utterance. Every ms here is added
      // latency before STT even starts, so keep it tight (was 800ms).
      silenceMs: 500,
      minSpeechMs: 300,
      maxIdleMs: 8000,
      onLevel: () => {},
      onSpeechStart: () => {},
      onFrame: () => {},
      ...options,
    };
  }

  async start(): Promise<void> {
    this.codec = pickCodec();
    if (!this.codec) throw new Error('MediaRecorder is not supported in this browser');
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const AC = window.AudioContext || (window as any).webkitAudioContext;
    this.ctx = new AC();
    // A fresh context can start suspended; the AnalyserNode only advances while
    // running, so resume before the VAD loop or it would never see speech.
    if (this.ctx.state === 'suspended') { try { await this.ctx.resume(); } catch { /* gesture-gated */ } }
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 512;
    src.connect(this.analyser);
    // Streaming (ws) mode: forward captured PCM continuously as it arrives. This
    // is additive to the per-utterance MediaRecorder path (which still emits the
    // authoritative is_final:true segment on VAD end-of-utterance).
    if (this.hasFrameSink) {
      const tap = this.ctx.createScriptProcessor(4096, 1, 1);
      const sink = this.ctx.createGain();
      sink.gain.value = 0; // never play the mic back — the tap only needs to run
      tap.onaudioprocess = (e: AudioProcessingEvent) => {
        if (this.stopped) return;
        const input = e.inputBuffer.getChannelData(0);
        try {
          this.opts.onFrame(float32ToPcm16Base64(input), this.ctx!.sampleRate);
        } catch {
          /* drop a bad frame rather than break capture */
        }
      };
      src.connect(tap);
      tap.connect(sink);
      sink.connect(this.ctx.destination);
      this.frameTap = tap;
      this.frameTapSink = sink;
    }
    this.stopped = false;
    this.arm();
    // setInterval (not rAF) so the VAD keeps ticking in background/headless tabs.
    this.timer = setInterval(this.tick, 50);
    // Progressive enhancement: bring the neural engine up in the background.
    // RMS is live from the first tick either way, so a slow model download (or
    // a failed one) never delays or breaks the call.
    void this.initSilero();
  }

  private async initSilero(): Promise<void> {
    if (!this.stream || !this.ctx) return;
    try {
      const engine = await SileroVadEngine.create({
        stream: this.stream,
        audioContext: this.ctx,
        redemptionMs: this.opts.silenceMs,
        minSpeechMs: this.opts.minSpeechMs,
        onSpeechStart: () => {
          if (this.stopped || this.engine !== 'silero') return;
          const now = performance.now();
          this.lastVoiceAt = now;
          if (!this.hadSpeech) {
            this.hadSpeech = true;
            this.speechStartAt = now;
            this.opts.onSpeechStart(); // barge-in: caller halts bot audio if it's speaking
          }
        },
        onSpeechEnd: (pcm16) => {
          if (this.stopped || this.engine !== 'silero') return;
          this.lastVoiceAt = performance.now();
          // vad-web has already enforced minSpeechMs; this segment is real.
          if (this.hadSpeech) {
            try {
              this.pendingPcm16B64 = float32ToPcm16Base64(pcm16);
            } catch {
              this.pendingPcm16B64 = undefined;
            }
            this.finalizeSegment(true);
          }
        },
        onMisfire: () => {
          if (this.stopped || this.engine !== 'silero') return;
          // Too short to be speech — recycle the segment without emitting so
          // the next real utterance starts from a clean recorder.
          if (this.hadSpeech) this.finalizeSegment(false);
        },
      });
      if (this.stopped) {
        engine.destroy(); // stop() raced the model load
        return;
      }
      this.silero = engine;
      this.engine = 'silero';
      // If an RMS-tracked utterance is in flight at switchover, close it out
      // now: the neural engine only judges audio from this point on, so no
      // other path would ever finalize that segment.
      if (this.hadSpeech) {
        this.finalizeSegment(this.lastVoiceAt - this.speechStartAt >= this.opts.minSpeechMs);
      }
    } catch {
      // Asset missing / WASM blocked / old browser — RMS stays in charge.
      this.engine = 'rms';
    }
  }

  get analyserNode(): AnalyserNode | null {
    return this.analyser;
  }

  private arm(): void {
    if (this.stopped || !this.stream || !this.codec) return;
    this.chunks = [];
    this.hadSpeech = false;
    this.segmentStartAt = performance.now();
    const rec = new MediaRecorder(this.stream, { mimeType: this.codec.mime });
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) this.chunks.push(e.data);
    };
    rec.onstop = () => {
      const emit = this.pendingEmit;
      this.pendingEmit = false;
      const pcm16 = this.pendingPcm16B64;
      this.pendingPcm16B64 = undefined;
      const blob = new Blob(this.chunks, { type: this.codec!.mime });
      if (emit && blob.size > 0) {
        blobToBase64(blob)
          .then((b64) => this.opts.onUtterance(b64, this.codec!.format, pcm16))
          .catch((err) => this.opts.onError?.(err));
      }
      if (!this.stopped) this.arm(); // re-arm for the next utterance
    };
    rec.start();
    this.recorder = rec;
  }

  private pendingEmit = false;

  private finalizeSegment(emit: boolean): void {
    this.pendingEmit = emit;
    const rec = this.recorder;
    this.recorder = null;
    if (rec && rec.state !== 'inactive') rec.stop(); // onstop emits + re-arms
  }

  private tick = (): void => {
    if (this.stopped || !this.analyser) return;
    const buf = new Uint8Array(this.analyser.fftSize);
    this.analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    this.opts.onLevel(rms);

    const now = performance.now();
    if (this.engine === 'silero') {
      // The neural engine owns speech start/end; this loop still meters level
      // (above) and recycles long idle segments to bound recorder memory.
      if (!this.hadSpeech && now - this.segmentStartAt >= this.opts.maxIdleMs) {
        this.finalizeSegment(false);
      }
      return;
    }
    // Speech must clear the ambient floor by a margin, but never drop below the
    // absolute thresholdRms — that floor keeps a dead-silent room from letting
    // noiseFloor collapse toward 0 and becoming hypersensitive to any faint sound.
    const speechThreshold = Math.max(this.opts.thresholdRms, this.noiseFloor * this.opts.noiseMultiplier);
    if (rms >= speechThreshold) {
      this.lastVoiceAt = now;
      if (!this.hadSpeech) {
        this.hadSpeech = true;
        this.speechStartAt = now;
        this.opts.onSpeechStart(); // barge-in: caller halts bot audio if it's speaking
      }
    } else if (this.hadSpeech && now - this.lastVoiceAt >= this.opts.silenceMs) {
      // End of utterance — emit only if the speech was long enough to be real.
      this.finalizeSegment(this.lastVoiceAt - this.speechStartAt >= this.opts.minSpeechMs);
    } else if (!this.hadSpeech && now - this.segmentStartAt >= this.opts.maxIdleMs) {
      this.finalizeSegment(false); // recycle idle silence to bound memory
    } else if (!this.hadSpeech) {
      // Only adapt while there's no candidate speech in progress, so a loud
      // utterance's own trailing decay is never absorbed into the noise floor.
      this.noiseFloor += (rms - this.noiseFloor) * this.opts.noiseFloorAlpha;
    }
  };

  stop(): void {
    this.stopped = true;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    this.silero?.destroy();
    this.silero = null;
    this.engine = 'rms';
    if (this.frameTap) {
      try { this.frameTap.onaudioprocess = null; this.frameTap.disconnect(); } catch { /* already gone */ }
      this.frameTap = null;
    }
    if (this.frameTapSink) {
      try { this.frameTapSink.disconnect(); } catch { /* already gone */ }
      this.frameTapSink = null;
    }
    const rec = this.recorder;
    this.recorder = null;
    if (rec && rec.state !== 'inactive') {
      this.pendingEmit = false;
      try {
        rec.stop();
      } catch {
        /* already stopped */
      }
    }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.ctx && this.ctx.state !== 'closed') void this.ctx.close();
    this.ctx = null;
    this.analyser = null;
  }
}
