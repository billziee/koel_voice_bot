// Real microphone capture with energy-based VAD endpointing.
//
// Runs a MediaRecorder continuously and uses an AnalyserNode to detect
// speech→silence boundaries. Each detected utterance is stopped, encoded
// (webm/opus where supported), base64'd, and handed to `onUtterance` for the
// live call to forward to STT (Groq Whisper). Leading silence is harmless —
// Whisper trims it — and re-arming happens in `onstop` so segments never overlap.

export type UtteranceHandler = (audioB64: string, format: string) => void;

export interface MicCaptureOptions {
  onUtterance: UtteranceHandler;
  onLevel?: (rms: number) => void; // 0..1, for the waveform / mic meter
  onError?: (err: unknown) => void;
  thresholdRms?: number; // speech vs. silence
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
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
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

  // VAD state
  private hadSpeech = false;
  private lastVoiceAt = 0;
  private speechStartAt = 0;
  private segmentStartAt = 0;

  constructor(options: MicCaptureOptions) {
    this.opts = {
      thresholdRms: 0.025,
      silenceMs: 800,
      minSpeechMs: 300,
      maxIdleMs: 8000,
      onLevel: () => {},
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
    this.stopped = false;
    this.arm();
    // setInterval (not rAF) so the VAD keeps ticking in background/headless tabs.
    this.timer = setInterval(this.tick, 50);
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
      const blob = new Blob(this.chunks, { type: this.codec!.mime });
      if (emit && blob.size > 0) {
        blobToBase64(blob)
          .then((b64) => this.opts.onUtterance(b64, this.codec!.format))
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
    if (rms >= this.opts.thresholdRms) {
      this.lastVoiceAt = now;
      if (!this.hadSpeech) {
        this.hadSpeech = true;
        this.speechStartAt = now;
      }
    } else if (this.hadSpeech && now - this.lastVoiceAt >= this.opts.silenceMs) {
      // End of utterance — emit only if the speech was long enough to be real.
      this.finalizeSegment(this.lastVoiceAt - this.speechStartAt >= this.opts.minSpeechMs);
    } else if (!this.hadSpeech && now - this.segmentStartAt >= this.opts.maxIdleMs) {
      this.finalizeSegment(false); // recycle idle silence to bound memory
    }
  };

  stop(): void {
    this.stopped = true;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
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
