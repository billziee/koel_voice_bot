// KOEL backend client — REST + WebSocket call protocol.
//
// Mirrors koel.voicebot.ws.protocol on the backend. The dashboard authenticates
// over REST (JWT), starts a session, then drives a live call over the socket.

import type { ModelConnection, ModelConnectionKind, Org, PlatformRole, PlatformUser } from '../types';

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
  // Free-form per-bot config blob the backend persists (e.g. { stt_mode: 'chunk' | 'ws' }).
  config?: Record<string, unknown> | null;
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

// Exchange a Google ID token (from the Sign in with Google button) for a KOEL
// access token. The backend verifies it against Google's JWKS and resolves the
// verified email against the allowlist (only enabled when the backend has a
// client id set). Throws KoelApiError(401) for a non-allowlisted/unverified
// email, 403 when Google login is disabled server-side.
export async function loginWithGoogle(credential: string): Promise<string> {
  const res = await fetch(`${KOEL_API}/v1/auth/google`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ credential }),
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

// Decodes a JWT's payload segment client-side — NEVER signature-verified (that's
// the server's job on every request); used only to drive UI decisions (nav
// gating, refresh scheduling), never to authorize an action. Returns null on any
// malformed/missing segment rather than throwing, so a bad token just degrades
// the UI (e.g. hides the admin nav item) instead of crashing the app.
interface TokenClaims {
  sub?: string;
  role?: string;
  org_id?: string | null;
  exp?: number;
}

function decodeTokenClaims(token: string): TokenClaims | null {
  try {
    const payload = token.split('.')[1];
    if (!payload) return null;
    return JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')));
  } catch {
    return null;
  }
}

// Milliseconds until the JWT's `exp`, or null if it can't be decoded. Used to
// schedule a proactive refresh at a fraction of the remaining lifetime.
export function tokenExpiresInMs(token: string): number | null {
  const claims = decodeTokenClaims(token);
  return typeof claims?.exp === 'number' ? claims.exp * 1000 - Date.now() : null;
}

// The token's role claim (UI gating ONLY — e.g. showing the Admin nav item; the
// backend re-checks role on every request regardless of what the FE decodes).
export function tokenRole(token: string): string | null {
  return decodeTokenClaims(token)?.role ?? null;
}

// The token's org_id claim — null means platform admin ("sees all"). UI gating
// only (e.g. hiding the org picker for a tenant-scoped admin); the backend is
// the actual authority on what each request may reach.
export function tokenOrgId(token: string): string | null {
  return decodeTokenClaims(token)?.org_id ?? null;
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

// Auto node-builder (6.6): draft a conversation flow from a bot's RAG knowledge
// base via the LLM. Returns the raw {start, nodes} flow (the caller coerces it
// into a positioned Flow). Throws KoelApiError on 404 (no KB) / 503 (LLM off).
export async function generateFlow(
  token: string,
  opts: { ragIndex: string; useCase?: string; language?: string },
): Promise<unknown> {
  const res = await fetch(`${KOEL_API}/v1/knowledge/generate-flow`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({
      rag_index: opts.ragIndex,
      use_case: opts.useCase,
      language: opts.language ?? 'en',
    }),
  });
  const body = await jsonOrThrow(res);
  return body.flow;
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

// ---- Model connections (BYOK — Phase 4) -----------------------------------
// Org-scoped provider credentials + endpoints. The api_key is write-only: the
// backend never returns it, so responses only carry `api_key_set`. We map the
// snake_case wire shape to the camelCase `ModelConnection` the UI consumes.

interface BackendModelConnection {
  id: string;
  org_id: string;
  name: string;
  kind: ModelConnectionKind;
  provider: string;
  base_url: string | null;
  model: string | null;
  params: Record<string, unknown> | null;
  api_key_set: boolean;
  active: boolean;
  created_at: string;
}

function mapConnection(b: BackendModelConnection): ModelConnection {
  return {
    id: b.id,
    name: b.name,
    kind: b.kind,
    provider: b.provider,
    baseUrl: b.base_url ?? '',
    model: b.model ?? '',
    params: b.params ?? {},
    apiKeySet: b.api_key_set,
    active: b.active,
  };
}

// Create/patch input in camelCase. `apiKey` is write-only — omit it (or pass an
// empty string, which is dropped below) to leave a stored key unchanged on edit.
export interface ConnectionInput {
  name: string;
  kind: ModelConnectionKind;
  provider: string;
  baseUrl?: string;
  apiKey?: string;
  model?: string;
  params?: Record<string, unknown>;
}

export type ConnectionPatch = Partial<ConnectionInput>;

// Only serialise fields that were actually provided, mapping camelCase→snake_case.
// A blank/absent apiKey is never sent, so editing without retyping the key keeps
// the existing one (blank = "leave unchanged").
function connectionToBody(input: ConnectionPatch): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.kind !== undefined) body.kind = input.kind;
  if (input.provider !== undefined) body.provider = input.provider;
  if (input.baseUrl !== undefined) body.base_url = input.baseUrl;
  if (input.model !== undefined) body.model = input.model;
  if (input.params !== undefined) body.params = input.params;
  if (input.apiKey) body.api_key = input.apiKey; // truthy only — never send a blank key
  return body;
}

export interface ConnectionTestResult {
  ok: boolean;
  detail: string;
  latencyMs?: number;
}

export async function listConnections(
  token: string,
  kind?: ModelConnectionKind,
): Promise<ModelConnection[]> {
  const qs = kind ? `?kind=${encodeURIComponent(kind)}` : '';
  const rows: BackendModelConnection[] = await jsonOrThrow(
    await fetch(`${KOEL_API}/v1/model-connections${qs}`, { headers: authHeaders(token) }),
  );
  return rows.map(mapConnection);
}

export async function createConnection(token: string, input: ConnectionInput): Promise<ModelConnection> {
  const res = await fetch(`${KOEL_API}/v1/model-connections`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(connectionToBody(input)),
  });
  return mapConnection(await jsonOrThrow(res));
}

export async function updateConnection(
  token: string,
  id: string,
  patch: ConnectionPatch,
): Promise<ModelConnection> {
  const res = await fetch(`${KOEL_API}/v1/model-connections/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: authHeaders(token),
    body: JSON.stringify(connectionToBody(patch)),
  });
  return mapConnection(await jsonOrThrow(res));
}

export async function deactivateConnection(token: string, id: string): Promise<ModelConnection> {
  const res = await fetch(`${KOEL_API}/v1/model-connections/${encodeURIComponent(id)}/deactivate`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  return mapConnection(await jsonOrThrow(res));
}

export async function testConnection(token: string, id: string): Promise<ConnectionTestResult> {
  const body = await jsonOrThrow(
    await fetch(`${KOEL_API}/v1/model-connections/${encodeURIComponent(id)}/test`, {
      method: 'POST',
      headers: authHeaders(token),
    }),
  );
  return {
    ok: Boolean(body.ok),
    detail: String(body.detail ?? ''),
    latencyMs: typeof body.latency_ms === 'number' ? body.latency_ms : undefined,
  };
}

// ---- Org registry (backend koel/voicebot/api/orgs.py) ----------------------
// Platform-admin only (unscoped token, org_id=None); a tenant-scoped token 403s.

interface BackendOrg {
  id: string;
  name: string;
  slug: string;
  active: boolean;
  created_at: string | null;
}

function mapOrg(row: BackendOrg): Org {
  return { id: row.id, name: row.name, slug: row.slug, active: row.active, createdAt: row.created_at };
}

export async function listOrgs(token: string): Promise<Org[]> {
  const rows: BackendOrg[] = await jsonOrThrow(
    await fetch(`${KOEL_API}/v1/orgs`, { headers: authHeaders(token) }),
  );
  return rows.map(mapOrg);
}

export async function createOrg(token: string, name: string, slug: string): Promise<Org> {
  const res = await fetch(`${KOEL_API}/v1/orgs`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ name, slug }),
  });
  return mapOrg(await jsonOrThrow(res));
}

export async function deactivateOrg(token: string, id: string): Promise<Org> {
  const res = await fetch(`${KOEL_API}/v1/orgs/${encodeURIComponent(id)}/deactivate`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  return mapOrg(await jsonOrThrow(res));
}

// ---- User accounts (backend koel/voicebot/api/users.py) --------------------
// Any role="admin" token: a platform admin (org_id=None) manages any org; a
// tenant-scoped admin (org_id set) is confined server-side to their own org.

interface BackendUser {
  id: string;
  username: string | null;
  email: string | null;
  org_id: string | null;
  role: PlatformRole;
  active: boolean;
  invite_pending: boolean;
  created_at: string | null;
}

function mapUser(row: BackendUser): PlatformUser {
  return {
    id: row.id,
    username: row.username,
    email: row.email,
    orgId: row.org_id,
    role: row.role,
    active: row.active,
    invitePending: row.invite_pending,
    createdAt: row.created_at,
  };
}

export interface InviteUserInput {
  username: string;
  email: string;
  role?: PlatformRole;
  orgId?: string | null; // ignored server-side for a tenant-scoped admin (forced to their own org)
}

export interface InviteResult {
  user: PlatformUser;
  inviteUrl: string; // always present — the copyable fallback when email is dormant
  emailSent: boolean;
}

function mapInviteResult(body: { user: BackendUser; invite_url: string; email_sent: boolean }): InviteResult {
  return { user: mapUser(body.user), inviteUrl: body.invite_url, emailSent: body.email_sent };
}

// orgId: platform-admin-only filter (a tenant-scoped admin always sees just
// their own org server-side regardless of this param).
export async function listUsers(token: string, orgId?: string): Promise<PlatformUser[]> {
  const qs = orgId ? `?org_id=${encodeURIComponent(orgId)}` : '';
  const rows: BackendUser[] = await jsonOrThrow(
    await fetch(`${KOEL_API}/v1/users${qs}`, { headers: authHeaders(token) }),
  );
  return rows.map(mapUser);
}

export async function inviteUser(token: string, input: InviteUserInput): Promise<InviteResult> {
  const res = await fetch(`${KOEL_API}/v1/users/invite`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({
      username: input.username,
      email: input.email,
      role: input.role ?? 'operator',
      org_id: input.orgId ?? null,
    }),
  });
  return mapInviteResult(await jsonOrThrow(res));
}

export async function updateUser(
  token: string,
  id: string,
  patch: { role?: PlatformRole; active?: boolean },
): Promise<PlatformUser> {
  const res = await fetch(`${KOEL_API}/v1/users/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: authHeaders(token),
    body: JSON.stringify(patch),
  });
  return mapUser(await jsonOrThrow(res));
}

async function _userAction(token: string, id: string, action: string): Promise<PlatformUser> {
  const res = await fetch(`${KOEL_API}/v1/users/${encodeURIComponent(id)}/${action}`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  return mapUser(await jsonOrThrow(res));
}

export const deactivateUser = (token: string, id: string) => _userAction(token, id, 'deactivate');
export const reactivateUser = (token: string, id: string) => _userAction(token, id, 'reactivate');

export async function resetUserPassword(token: string, id: string): Promise<InviteResult> {
  const res = await fetch(`${KOEL_API}/v1/users/${encodeURIComponent(id)}/reset-password`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  return mapInviteResult(await jsonOrThrow(res));
}

export async function resendInvite(token: string, id: string): Promise<InviteResult> {
  const res = await fetch(`${KOEL_API}/v1/users/${encodeURIComponent(id)}/resend-invite`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  return mapInviteResult(await jsonOrThrow(res));
}

// Public — no auth. Redeems an invite/reset token and sets a password; returns
// an access token so the acceptor lands logged in without a second login step.
export async function acceptInvite(token: string, password: string): Promise<string> {
  const res = await fetch(`${KOEL_API}/v1/users/accept-invite`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, password }),
  });
  const body = await jsonOrThrow(res);
  return body.access_token as string;
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
  | 'call_state' | 'transcript' | 'partial_transcript' | 'audio_chunk' | 'tts_kill' | 'filler'
  | 'reconnected' | 'ping' | 'error' | 'call_ended';

export interface ServerFrame {
  type: ServerFrameType;
  data: Record<string, any>;
}

export interface LiveCallHandlers {
  onCallState?: (status: string, data: Record<string, any>) => void;
  onTranscript?: (speaker: 'bot' | 'customer', text: string, data: Record<string, any>) => void;
  // Wave 2: best-effort LIVE interim transcript. Revised/replaced as more audio
  // arrives; superseded by the authoritative `transcript` frame for the turn.
  onPartialTranscript?: (speaker: 'bot' | 'customer', text: string, data: Record<string, any>) => void;
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
      case 'partial_transcript': {
        // Tolerate either the nested `{type,data:{speaker,text}}` protocol shape
        // (like every other frame) or a flattened `{type,speaker,text}` payload.
        const flat = frame as ServerFrame & { speaker?: string; text?: string };
        const speaker = (d.speaker ?? flat.speaker) as 'bot' | 'customer';
        const text = (d.text ?? flat.text ?? '') as string;
        this.handlers.onPartialTranscript?.(speaker, text, d);
        break;
      }
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

  /** Streaming (ws) mode only: forward a captured PCM16 frame mid-utterance as a
   *  NON-final chunk (`is_final:false`) so the backend can run incremental STT.
   *  The authoritative `is_final:true` send still fires at VAD end-of-utterance
   *  via sendAudioChunk — this is purely additive. */
  sendAudioFrame(pcm16B64: string, sampleRate = 16000): void {
    this.send({
      type: 'audio_chunk',
      audio_b64: pcm16B64,
      is_final: false,
      audio_format: 'pcm16',
      sample_rate: sampleRate,
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
