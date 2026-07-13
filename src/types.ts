// Shared UI domain types for the KOEL dashboard.
//
// Extracted from App.tsx (roadmap Q-1 decomposition) so view modules and hooks
// can share one canonical set of types instead of reaching into the monolith.

export type ViewState = 'dashboard' | 'call' | 'config' | 'history' | 'builder' | 'knowledge';
export type CallState =
  | 'idle' | 'connecting' | 'bot_speaking' | 'listening' | 'user_speaking'
  | 'processing' | 'reconnecting' | 'escalating' | 'ended';
export type Tone = 'cheerful' | 'empathetic' | 'firm' | 'neutral';
export type UseCase = 'Credit Card' | 'Collections' | 'Insurance' | 'Personal Loan';
export type AmbientProfile = 'callfloor' | 'office' | 'comfort' | 'silent';
// STT pipeline mode — operator choice, persists to backend `config.stt_mode`.
// 'chunk' (default) = batch each utterance; 'ws' = stream over a persistent socket.
export type SttMode = 'chunk' | 'ws';

// ── Conversation flow (roadmap 6.6) ──────────────────────────────────────────
// A scripted state machine the backend interpreter consumes from
// BotConfig.config["flow"] (see backend koel/voicebot/pipeline/flow.py). It
// scaffolds the LLM — each node's `prompt` is per-turn guidance and `branches`
// route on the NLP-returned intent. These intent values mirror the backend
// `Intent` enum exactly; `'*'` is the wildcard fallback branch.
export type FlowIntent =
  | 'high_interest' | 'interested' | 'objection' | 'not_interested'
  | 'busy' | 'promise_to_pay' | 'dispute' | 'request_human'
  | 'opt_out' | 'smalltalk' | 'unknown';
export type FlowBranchKey = FlowIntent | '*';

// Editor-only board position for a node. Persisted under the node's private
// `_ui` key: the backend interpreter reads only `prompt`/`branches`, so it
// ignores `_ui` — positions round-trip through config without touching a call.
export interface FlowNodeUi { x: number; y: number }

export interface FlowNode {
  prompt: string; // per-turn guidance appended to (never replacing) the system prompt
  branches: Record<string, string>; // intent value (or '*') → next node id
  _ui?: FlowNodeUi; // editor-only position; backend safely ignores it
}

export interface Flow {
  start: string; // must resolve to an existing node id
  nodes: Record<string, FlowNode>;
}

export interface Bot {
  id: string;
  name: string;
  useCase: UseCase;
  language: string;
  status: 'Active' | 'Paused';
  calls: number;
  conversionNum: number;
  conversion: string;
  avgDuration: string;
  escalationRate: string;
  lastActive: string;
  lastActiveTs: number; // epoch for live aging
  backendId?: string; // real VB_Svc bot id when connected to the live backend
  backendCustomerId?: string; // seed CRM customer to dial for this bot
  sttMode?: SttMode; // hydrated from backend `config.stt_mode` so the config form can seed it
  flow?: Flow; // hydrated from backend `config.flow` so the flow editor can seed it (6.6)
}

// Item 5 — per-bot configuration that persists across views
export interface BotConfig {
  name: string;
  useCase: string;
  language: string;
  persona: string;
  speed: number;
  ambientVolume: number;
  ambientProfile: AmbientProfile;
  backchannels: string[];
  ragEnabled: boolean;
  ragIndex: string;
  greeting: string;
  tone: Tone;
  sttMode: SttMode; // Item — operator-selected STT pipeline; round-trips to backend config.stt_mode
  flow?: Flow; // 6.6 — scripted conversation flow; round-trips to backend config.flow
}

export interface CRMField { label: string; value: string; highlight?: 'green' | 'orange' }
export interface CRMProfile {
  name: string;
  customerId: string;
  phone: string;
  segment: string;
  fields: CRMField[];
}

export interface LatencyBreakdown { stt: number; nlp: number; tts: number; total: number }

export interface TranscriptMessageData {
  id: number;
  kind: 'bot' | 'user' | 'meta' | 'filler';
  text: string;
  tone?: Tone;
  latency?: LatencyBreakdown;
  interrupted?: boolean;
  timestamp?: number; // Item 58 — offset seconds from call start
}

export interface CallHistory {
  id: string;
  bot: string;
  language: string;
  durationStr: string;
  durationSec: number;
  outcome: string;
  dateStr: string;
  timestamp: number;
  status: 'green' | 'orange' | 'red' | 'gray';
  customer: string;
  customerId: string; // Item 16
  phone: string;
  avgLatencyMs: number;
  transcript: TranscriptMessageData[];
  events?: SessionEvent[]; // Item 35
}

export interface PastInteraction {
  id: string;
  date: string;
  topic: string;
  duration: string;
  transcript: { speaker: 'bot' | 'user'; text: string }[];
}

export interface ChipDef {
  label: string;
  userText: string;
  botReply: string;
  tone: Tone;
  ragContext?: string;
  nlpSlow?: boolean;
  action?: 'ESCALATE' | 'END_POLITE';
  outcome?: { label: string; status: CallHistory['status'] };
  consentCapture?: string; // Item 17 — IRDAI consent event
}

export interface Scenario {
  greeting: string;
  greetingTone: Tone;
  rePrompt: string;
  politeClose: string;
  bargeInUserText: string;
  bargeInReply: string;
  chips: ChipDef[];
}

export interface SessionEvent { id: number; t: string; label: string; level: 'info' | 'warn' | 'ok' }
