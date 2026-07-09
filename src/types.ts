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
