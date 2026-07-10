// KOEL backend client — REST + WebSocket call protocol.
//
// Mirrors koel.voicebot.ws.protocol on the backend. The dashboard authenticates
// over REST (JWT), starts a session, then drives a live call over the socket.

export const KOEL_API: string =
  (import.meta as any).env?.VITE_KOEL_API || 'http://localhost:8100';

const WS_BASE = KOEL_API.replace(/^http/, 'ws');

// ---- REST shapes -----------------------------------------------------------

export interface BackendBot {
  id: string;
  name: string;
  use_case: 'credit_card' | 'collections' | 'personal_loan' | 'insurance' | 'custom';
  language: string;
  default_tone: string;
  rag_enabled: boolean;
  rag_index: string | null;
  active: boolean;
}

export interface BackendCustomer {
  id: string;
  name: string;
  phone: string;
  language: string;
  bank: string;
  use_case: string;
  dnd: boolean;
  attributes: Record<string, unknown>;
}

export interface StartSessionResult {
  session_id: string;
  ws_token: string;
  ws_url: string;
  crm_context: Record<string, unknown>;
  language: string;
  use_case: string;
}

async function jsonOrThrow(res: Response): Promise<any> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const code = body?.code || res.status;
    const message = body?.message || res.statusText;
    throw new KoelApiError(res.status, String(code), String(message));
  }
  return body;
}

export class KoelApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
    this.name = 'KoelApiError';
  }
}

export async function login(
  username: string,
  password: string,
  turnstileToken?: string | null,
): Promise<string> {
  const res = await fetch(`${KOEL_API}/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // turnstile_token is only enforced by the backend when it has a secret key
    // configured (6.7); harmless to send null otherwise.
    body: JSON.stringify({ username, password, turnstile_token: turnstileToken ?? null }),
  });
  const body = await jsonOrThrow(res);
  return body.access_token as string;
}

// Silent refresh (Q-2): exchange a still-valid access token for a fresh one via
// POST /v1/auth/refresh. Throws (KoelApiError) if the token is expired/invalid —
// the caller then forces re-login. There is no separate refresh token; the
// short-TTL access token is the credential, so persist + refresh it directly.
export async function refreshToken(token: string): Promise<string> {
  const res = await fetch(`${KOEL_API}/v1/auth/refresh`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  const body = await jsonOrThrow(res);
  return body.access_token as string;
}

// --- Access-token persistence (Q-2) -----------------------------------------
// Stored in localStorage so a page reload doesn't log the operator out. Trade-off
// (documented): localStorage is readable by XSS. The backend issues a short-TTL
// (15 min) access token and we refresh silently, so the exposure window is bounded;
// httpOnly cookies would need a backend change (login returns the token in JSON).
const TOKEN_STORAGE_KEY = 'koel.access_token';

export function saveToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    /* private mode / storage disabled — degrade to in-memory only */
  }
}

export function loadStoredToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function clearStoredToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* nothing to clear */
  }
}

// Milliseconds until the JWT's `exp`, or null if it can't be decoded. Only the
// payload is base64-decoded (never signature-verified — that's the server's job);
// used to schedule a proactive refresh at a fraction of the remaining lifetime.
export function tokenExpiresInMs(token: string): number | null {
  try {
    const payload = token.split('.')[1];
    if (!payload) return null;
    const claims = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')));
    return typeof claims.exp === 'number' ? claims.exp * 1000 - Date.now() : null;
  } catch {
    return null;
  }
}

function authHeaders(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
}

export async function getBots(token: string): Promise<BackendBot[]> {
  return jsonOrThrow(await fetch(`${KOEL_API}/v1/bots`, { headers: authHeaders(token) }));
}

// Deterministic seed-customer per use case (matches scripts/bootstrap_db.py).
export const USE_CASE_CUSTOMER: Record<string, string> = {
  credit_card: 'cust_cc_001',
  collections: 'cust_col_001',
  personal_loan: 'cust_loan_001',
  insurance: 'cust_ins_001',
};

export interface NewBotPayload {
  name: string;
  use_case: 'credit_card' | 'collections' | 'personal_loan' | 'insurance' | 'custom';
  language: string;
  voice_gender?: string;
  speaking_rate?: number;
  greeting_template?: string;
  rag_enabled?: boolean;
  rag_index?: string | null;
  default_tone?: string;
  ambient_profile?: string;
  config?: Record<string, unknown>;
}

export async function createBot(token: string, payload: NewBotPayload): Promise<BackendBot> {
  const res = await fetch(`${KOEL_API}/v1/bots`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
  return jsonOrThrow(res);
}

// Partial in-place edit (S/AF) — server persists + writes an audit row.
export type BotConfigPatch = Partial<Omit<NewBotPayload, 'use_case'>> & { active?: boolean };

export async function patchBot(token: string, botId: string, patch: BotConfigPatch): Promise<BackendBot> {
  const res = await fetch(`${KOEL_API}/v1/bots/${encodeURIComponent(botId)}`, {
    method: 'PATCH',
    headers: authHeaders(token),
    body: JSON.stringify(patch),
  });
  return jsonOrThrow(res);
}

// ---- Analytics (AE) --------------------------------------------------------

export interface BotStats {
  bot_id: string;
  total_calls: number;
  converted: number;
  escalated: number;
  conversion_rate: number; // 0..1
  escalation_rate: number; // 0..1
  avg_duration_ms: number;
  outcomes: Record<string, number>;
}

export async function getBotStats(token: string, botId: string): Promise<BotStats> {
  return jsonOrThrow(
    await fetch(`${KOEL_API}/v1/bots/${encodeURIComponent(botId)}/stats`, {
      headers: authHeaders(token),
    }),
  );
}

export async function getAllStats(token: string): Promise<BotStats[]> {
  return jsonOrThrow(await fetch(`${KOEL_API}/v1/stats`, { headers: authHeaders(token) }));
}

// ---- Knowledge base admin (X) ---------------------------------------------

export interface KnowledgeIndex {
  rag_index: string;
  chunk_count: number;
}

export async function getKnowledgeIndexes(token: string): Promise<KnowledgeIndex[]> {
  return jsonOrThrow(
    await fetch(`${KOEL_API}/v1/knowledge-indexes`, { headers: authHeaders(token) }),
  );
}

export interface NewKnowledgeChunk {
  rag_index: string;
  use_case: string;
  language?: string;
  title: string;
  content: string;
  chunk?: boolean;
}

export async function createKnowledgeChunk(token: string, payload: NewKnowledgeChunk): Promise<unknown> {
  return jsonOrThrow(
    await fetch(`${KOEL_API}/v1/knowledge-chunks`, {
      method: 'POST',
      headers: authHeaders(token),
      body: JSON.stringify(payload),
    }),
  );
}

// Upload a text/PDF file → server extracts, chunks, embeds, and stores it.
export async function uploadKnowledgeFile(
  token: string,
  opts: { file: File; ragIndex: string; useCase: string; language?: string },
): Promise<unknown[]> {
  const fd = new FormData();
  fd.append('file', opts.file);
  fd.append('rag_index', opts.ragIndex);
  fd.append('use_case', opts.useCase);
  fd.append('language', opts.language ?? 'hinglish');
  // No Content-Type header — the browser sets the multipart boundary itself.
  return jsonOrThrow(
    await fetch(`${KOEL_API}/v1/knowledge-files`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    }),
  );
}

export async function startSession(
  token: string,
  botId: string,
  customerId: string | null,
): Promise<StartSessionResult> {
  const res = await fetch(`${KOEL_API}/v1/sessions`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ bot_id: botId, customer_id: customerId }),
  });
  return jsonOrThrow(res);
}

// ---- WebSocket protocol ----------------------------------------------------

export type ServerFrameType =
  | 'call_state' | 'transcript' | 'audio_chunk' | 'tts_kill' | 'filler'
  | 'reconnected' | 'ping' | 'error' | 'call_ended';

export interface ServerFrame {
  type: ServerFrameType;
  data: Record<string, any>;
}

export interface LiveCallHandlers {
  onCallState?: (status: string, data: Record<string, any>) => void;
  onTranscript?: (speaker: 'bot' | 'customer', text: string, data: Record<string, any>) => void;
  onAudio?: (data: Record<string, any>) => void;
  onFiller?: (text: string, data: Record<string, any>) => void;
  onTtsKill?: (reason: string) => void;
  onReconnected?: (text: string, data: Record<string, any>) => void;
  onError?: (code: string, message: string) => void;
  onEnded?: (outcome: string, data: Record<string, any>) => void;
  onSocketClose?: (clean: boolean) => void;
  onOpen?: () => void;
}

/** Wraps one WebSocket call session, auto-replying to heartbeats. */
export class LiveCall {
  private ws: WebSocket | null = null;
  private handlers: LiveCallHandlers;
  readonly sessionId: string;
  private readonly wsToken: string;

  constructor(sessionId: string, wsToken: string, handlers: LiveCallHandlers) {
    this.sessionId = sessionId;
    this.wsToken = wsToken;
    this.handlers = handlers;
  }

  connect(): void {
    // Send the WS JWT via the Sec-WebSocket-Protocol subprotocol, not the URL, so
    // it never lands in server access logs / browser history (M). The server
    // echoes 'koel.v1' to complete the handshake.
    const url = `${WS_BASE}/ws/calls/${this.sessionId}`;
    const ws = new WebSocket(url, ['koel.v1', `koel.token.${this.wsToken}`]);
    this.ws = ws;
    ws.onopen = () => this.handlers.onOpen?.();
    ws.onclose = (e) => this.handlers.onSocketClose?.(e.wasClean);
    ws.onerror = () => this.handlers.onError?.('socket_error', 'WebSocket connection error.');
    ws.onmessage = (e) => {
      let frame: ServerFrame;
      try {
        frame = JSON.parse(e.data) as ServerFrame;
      } catch {
        this.handlers.onError?.('bad_frame', 'Received a malformed server frame.');
        return;
      }
      this.dispatch(frame);
    };
  }

  private dispatch(frame: ServerFrame): void {
    const d = frame.data || {};
    switch (frame.type) {
      case 'ping':
        this.send({ type: 'pong' });
        break;
      case 'call_state':
        this.handlers.onCallState?.(d.status, d);
        break;
      case 'transcript':
        this.handlers.onTranscript?.(d.speaker, d.text, d);
        break;
      case 'audio_chunk':
        this.handlers.onAudio?.(d);
        break;
      case 'filler':
        this.handlers.onFiller?.(d.text, d);
        break;
      case 'tts_kill':
        this.handlers.onTtsKill?.(d.reason);
        break;
      case 'reconnected':
        this.handlers.onReconnected?.(d.text, d);
        break;
      case 'error':
        this.handlers.onError?.(d.code, d.message);
        break;
      case 'call_ended':
        this.handlers.onEnded?.(d.outcome, d);
        break;
    }
  }

  private send(obj: Record<string, any>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  /** Send a customer utterance. The STT stub returns it verbatim (type-to-talk). */
  sendText(text: string, sampleRate = 16000): void {
    const audio_b64 = btoa(unescape(encodeURIComponent('TEXT:' + text)));
    this.send({ type: 'audio_chunk', audio_b64, is_final: true, sample_rate: sampleRate });
  }

  /** Send a captured microphone utterance (base64 webm/mp4/ogg) to real STT
   *  (Whisper). pcm16B64 (s16le mono 16kHz, from the Silero VAD worklet) rides
   *  along when available — it powers the server's semantic end-of-turn gate. */
  sendAudioChunk(audioB64: string, format = 'webm', pcm16B64?: string): void {
    this.send({
      type: 'audio_chunk',
      audio_b64: audioB64,
      is_final: true,
      audio_format: format,
      ...(pcm16B64 ? { pcm16_b64: pcm16B64 } : {}),
    });
  }

  /** Gate-1 barge-in candidate fired while the bot is speaking. */
  bargeIn(partialText: string, speechDurationMs: number, language = 'hinglish'): void {
    this.send({
      type: 'barge_in_candidate',
      data: { partial_text: partialText, speech_duration_ms: speechDurationMs, language },
    });
  }

  /** Tell the server the audio queue drained — the bot has truly stopped
   *  speaking, so the no-input timer should start from now (not the estimate). */
  playbackDone(): void {
    this.send({ type: 'playback_done' });
  }

  endCall(): void {
    this.send({ type: 'end_call' });
  }

  close(): void {
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      // Detach handlers first so no late frames/close events fire into a
      // component that may already be unmounted (prevents zombie callbacks).
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try {
        ws.close();
      } catch {
        /* noop */
      }
    }
  }
}
