import React, { useState, useEffect, useRef } from 'react';
import {
  Square, Mic, PhoneCall, User, Database, Activity, ShieldAlert, Zap,
  MessageSquare, ChevronRight, ChevronDown, CheckCircle2, AlertCircle,
  WifiOff, Languages, Timer, Volume2, Radio,
} from 'lucide-react';
import {
  startSession as apiStartSession,
  LiveCall,
  KoelApiError,
} from '../lib/koel';
import { MicCapture, micSupported } from '../lib/audio';
import type {
  CallState, Tone, UseCase, Bot, CRMProfile, LatencyBreakdown,
  TranscriptMessageData, CallHistory, PastInteraction, ChipDef, Scenario, SessionEvent,
} from '../types';
import type { Theme } from '../lib/theme';
import { formatTime } from '../lib/format';
import { TranscriptMessage } from '../components/TranscriptMessage';

// ActiveCallView + its call-only sub-components and mock data, extracted from
// App.tsx (roadmap Q-1 decomposition). Behaviour is unchanged; the component
// still receives its bot/theme/token via props and owns the demo + live-call
// state machine internally.

const MOCK_CUSTOMERS: Record<UseCase, CRMProfile> = {
  'Credit Card': {
    name: 'Rahul Sharma', customerId: 'CUST-882910', phone: '+91 98*** ***21', segment: 'High Net Worth',
    fields: [
      { label: 'Pre-Approved Limit', value: '₹5,00,000', highlight: 'green' },
      { label: 'Eligible Offer', value: 'Regalia Gold Card', highlight: 'green' },
      { label: 'Offer Type', value: 'Pre-Approved · FYF' },
      { label: 'Credit Score', value: '810' },
      { label: 'Recent Activity', value: 'Web login 2 days ago' },
    ]
  },
  'Collections': {
    name: 'Amit Patel', customerId: 'CUST-554102', phone: '+91 99*** ***44', segment: 'Salaried',
    fields: [
      { label: 'Amount Due', value: '₹ 12,500', highlight: 'orange' },
      { label: 'DPD', value: '0 (Pre-Due)' },
      { label: 'Due Date', value: '15 Jun 2026' },
      { label: 'Payment History', value: '11/12 on time' },
    ]
  },
  'Insurance': {
    name: 'Vikram Desai', customerId: 'CUST-119283', phone: '+91 76*** ***90', segment: 'Self-Employed',
    fields: [
      { label: 'Product Interest', value: 'Term Plan ₹1 Cr', highlight: 'green' },
      { label: 'Quoted Premium', value: '₹ 1,450 / month' },
      { label: 'Age Band', value: '32–35' },
      { label: 'Recent Activity', value: 'Premium calculator, app' },
    ]
  },
  'Personal Loan': {
    name: 'Sneha Iyer', customerId: 'CUST-773645', phone: '+91 88*** ***07', segment: 'Pre-Screened',
    fields: [
      { label: 'Pre-Approved Amount', value: '₹ 4,00,000', highlight: 'green' },
      { label: 'Indicative ROI', value: '10.75% p.a.' },
      { label: 'Max Tenure', value: '60 months' },
      { label: 'FOIR Headroom', value: '38%' },
    ]
  },
};

const PAST_INTERACTIONS: PastInteraction[] = [
  {
    id: 'pi-1', date: '12 May 2026', topic: 'KYC Update Request', duration: '1m 20s',
    transcript: [
      { speaker: 'bot', text: 'Namaste Rahul ji, aapka KYC pending hai. Kya online update kar lenge?' },
      { speaker: 'user', text: 'Haan, main aaj shaam tak app se kar dunga.' },
      { speaker: 'bot', text: 'Dhanyawad. Main aapko SMS pe link bhej rahi hoon.' }
    ]
  },
  {
    id: 'pi-2', date: '03 April 2026', topic: 'Welcome Call', duration: '2m 10s',
    transcript: [
      { speaker: 'bot', text: 'Welcome to HDFC Bank, Rahul ji. Aapki account details mail kar di gayi hain.' },
      { speaker: 'user', text: 'Thank you. Debit card kab aayega?' },
      { speaker: 'bot', text: 'Sir, debit card 7 working days mein aapke registered address par deliver ho jayega.' }
    ]
  }
];

// Item 17: Insurance consent capture added to "Send me the quote" chip
const SCENARIOS: Record<UseCase, Scenario> = {
  'Credit Card': {
    greeting: 'Namaste Rahul ji, main HDFC Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved Regalia credit card offer hai. Do minute baat kar sakte hain?',
    greetingTone: 'cheerful',
    rePrompt: 'Hello, aap sun pa rahe hain? Are you still there?',
    politeClose: 'Lagta hai aap busy hain. Main baad mein call karti hoon. Dhanyavaad.',
    bargeInUserText: 'Wait wait — pehle yeh batao isme charges kya hain?',
    bargeInReply: 'Bilkul sir. Pehle saal ki joining fee poori tarah waived hai, aur ₹3 lakh annual spend par renewal fee bhi waive ho jaati hai. Koi hidden charges nahi.',
    chips: [
      { label: '"I\'m busy right now"', userText: 'Abhi thoda busy hoon.', botReply: 'Koi baat nahi sir. Main aapko shaam ko 6 baje call kar leti hoon. Thank you.', tone: 'neutral', action: 'END_POLITE', outcome: { label: 'Callback Scheduled', status: 'gray' } },
      { label: '"I already have 2 cards"', userText: 'Haan boliye, but mere paas already do cards hain.', botReply: 'Bilkul samajh sakti hoon. Yeh card lounge access aur 4x reward points deta hai dining par — aapke existing cards se alag benefit. Pehle saal ki fee bhi waived hai. Interested hain?', tone: 'empathetic', ragContext: 'RAG: Objection Handling — Multiple Cards (hdfc_regalia_benefits_v2)', nlpSlow: true },
      { label: '"Talk to a human"', userText: 'Theek hai, thoda detail chahiye. Koi bande se baat kara do.', botReply: 'Zaroor. Main abhi aapko ek specialist se connect karti hoon jo poori detail denge. Ek second hold kijiye.', tone: 'cheerful', action: 'ESCALATE', outcome: { label: 'Escalated to Agent', status: 'orange' } },
    ]
  },
  'Collections': {
    greeting: 'Namaste Amit ji, main ICICI Bank ki taraf se KOEL bol rahi hoon — yeh ek automated reminder call hai. Aapke credit card ka ₹12,500 ka bill 15 June ko due hai. Kya aap payment plan confirm kar sakte hain?',
    greetingTone: 'firm',
    rePrompt: 'Hello, aap sun pa rahe hain? Are you still there?',
    politeClose: 'Lagta hai aap busy hain. Main baad mein call karti hoon. Dhanyavaad.',
    bargeInUserText: 'Ruko ruko — maine partial payment already kar di hai!',
    bargeInReply: 'Oh, dhanyavaad batane ke liye. Mujhe record mein ₹5,000 ki payment dikh rahi hai. Remaining ₹7,500 ke liye kya main aapko payment link bhej doon?',
    chips: [
      { label: '"I will pay tomorrow"', userText: 'Haan pata hai, main kal pay kar dunga.', botReply: 'Dhanyavaad confirm karne ke liye. Maine aapka promise-to-pay kal ke liye record kar liya hai. Convenience ke liye SMS par payment link bhi bhej rahi hoon. Have a great day!', tone: 'empathetic', ragContext: 'Action: PROMISE_TO_PAY recorded · SMS_PAYMENT_LINK_SENT', action: 'END_POLITE', outcome: { label: 'Promise to Pay', status: 'green' } },
      { label: '"Can I pay partially?"', userText: 'Poora nahi ho payega abhi. Aadha kar sakta hoon kya?', botReply: 'Ji bilkul. Aap minimum due ₹2,500 ya partial ₹6,000 abhi pay kar sakte hain — interest sirf remaining balance par lagega. Main link bhej doon?', tone: 'empathetic', ragContext: 'RAG: Partial Payment Policy (icici_collection_guidelines_2026)', nlpSlow: true },
      { label: '"This bill is wrong"', userText: 'Yeh bill galat hai, maine itna use hi nahi kiya.', botReply: 'Main samajh sakti hoon, billing dispute serious hai. Main aapko abhi dispute team se connect karti hoon — aapko dobara details repeat nahi karni padengi.', tone: 'firm', action: 'ESCALATE', outcome: { label: 'Dispute — Escalated', status: 'orange' } },
    ]
  },
  'Insurance': {
    greeting: 'Hello Vikram sir, this is KOEL calling from MaxLife — this is an automated call. You recently checked term insurance premiums on our app. Per IRDAI guidelines I must mention: insurance is subject to market and policy terms. May I share a quote that matches your profile?',
    greetingTone: 'cheerful',
    rePrompt: 'Hello, can you hear me? Are you still there?',
    politeClose: 'It seems you are busy. I will call back later. Thank you for your time.',
    bargeInUserText: 'Hold on — is the premium fixed or will it increase?',
    bargeInReply: 'Great question. The premium is locked for the entire policy term once issued — it never increases with age or claims. That is exactly why buying early saves money.',
    chips: [
      { label: '"Premium is too high"', userText: 'Yes, but the premium was a bit high.', botReply: 'I understand. If you opt for annual payments instead of monthly, you get a 5% discount. Plus, at your age, locking in this premium now saves you money long-term. Shall I send the revised quote on WhatsApp?', tone: 'empathetic', ragContext: 'RAG: Premium Objection Handling (maxlife_term_disclosures)', nlpSlow: true },
      { label: '"Send me the quote"', userText: 'Okay, send it on WhatsApp.', botReply: 'Done sir — the revised quote with the annual-payment discount is on its way to your WhatsApp. A licensed advisor will follow up for the final steps. Thank you!', tone: 'cheerful', ragContext: 'Action: WHATSAPP_QUOTE_SENT · Advisor callback booked', action: 'END_POLITE', outcome: { label: 'Completed — Interested', status: 'green' }, consentCapture: 'VERBAL_CONSENT captured · IRDAI compliance logged' },
      { label: '"Talk to an advisor"', userText: 'I would rather discuss this with a person.', botReply: 'Of course. I am connecting you to a licensed advisor right away — they will have this full conversation context. One moment please.', tone: 'neutral', action: 'ESCALATE', outcome: { label: 'Escalated to Advisor', status: 'orange' } },
    ]
  },
  'Personal Loan': {
    greeting: 'Namaste Sneha ji, main Axis Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke liye ₹4 lakh tak ka pre-approved personal loan offer hai, sirf 10.75% interest par. Kya do minute baat kar sakte hain?',
    greetingTone: 'cheerful',
    rePrompt: 'Hello, aap sun pa rahe hain? Are you still there?',
    politeClose: 'Lagta hai aap busy hain. Main baad mein call karti hoon. Dhanyavaad.',
    bargeInUserText: 'Ek minute — processing fee kitni hai iski?',
    bargeInReply: 'Sure ma\'am. Processing fee sirf 1% hai, aur is offer mein woh bhi 50% discounted hai. Koi prepayment penalty nahi hai 12 EMIs ke baad.',
    chips: [
      { label: '"What will my EMI be?"', userText: 'EMI kitni banegi 4 lakh par?', botReply: '₹4 lakh par 36 months ke liye EMI banegi lagbhag ₹13,050 per month. Agar tenure 60 months karein toh ₹8,650. Kaunsa option better lagta hai?', tone: 'neutral', ragContext: 'Dynamic EMI calculation: P=₹4,00,000 · r=10.75% · n=36/60', nlpSlow: true },
      { label: '"Book a callback"', userText: 'Theek hai, mujhe loan team se baat karni hai. Kal subah call karwa do.', botReply: 'Perfect ma\'am. Maine kal subah 10 baje loan team ka callback book kar diya hai. Aapko SMS confirmation mil jayega. Dhanyavaad!', tone: 'cheerful', ragContext: 'Action: CALLBACK_BOOKED — Loan team, 10:00 AM', action: 'END_POLITE', outcome: { label: 'Callback Booked', status: 'green' } },
      { label: '"Not interested"', userText: 'Nahi, mujhe abhi loan nahi chahiye.', botReply: 'Koi baat nahi ma\'am. Main aapki preference record kar leti hoon — aapko is offer ke liye dobara call nahi aayega. Apna din shubh ho!', tone: 'neutral', ragContext: 'CRM Flag: OPT_OUT honoured (TRAI compliance)', action: 'END_POLITE', outcome: { label: 'Declined — Opt Out', status: 'red' } },
    ]
  },
};

// Item 20: Fixed seeded history greetings — all include automated self-identification

const FILLER_POOLS: Record<string, string[]> = {
  Hinglish: ['"Hmm… ek second rukiye…"', '"Ji, main abhi check karti hoon…"', '"Achha, bas ek moment dijiye…"', '"Ek minute, main dekh rahi hoon…"', '"Ji bilkul, abhi batati hoon…"', '"Haan ji, bas do second…"'],
  Hindi: ['"Hmm… ek second rukiye…"', '"Ji, main abhi dekh rahi hoon…"', '"Bas ek pal rukiye…"', '"Ek minute dijiye, abhi batati hoon…"', '"Ji, zaroor — bas ek second…"'],
  English: ['"Hmm… let me pull that up…"', '"One moment please…"', '"Right, just a second…"', '"Bear with me a moment…"', '"Let me just check that for you…"'],
};

const randLatency = (slowNlp = false): LatencyBreakdown => {
  const stt = 55 + Math.floor(Math.random() * 35);
  const nlp = slowNlp ? 950 + Math.floor(Math.random() * 600) : 95 + Math.floor(Math.random() * 70);
  const tts = 60 + Math.floor(Math.random() * 35);
  return { stt, nlp, tts, total: stt + nlp + tts };
};

const LANG_CODES: Record<string, string> = { Hinglish: 'hi-Latn', Hindi: 'hi', English: 'en' };

let msgId = 1000;
const nextId = () => ++msgId;


// ============================================================================
// Root App — login gate + view router
// ============================================================================
// Map a backend use_case to the frontend UseCase label used by SCENARIOS/CRM.

function AgentHandoffPanel({ theme, darkMode }: { theme: Theme, darkMode: boolean }) {
  const [step, setStep] = useState(0);

  useEffect(() => {
    const t1 = setTimeout(() => setStep(1), 600);
    const t2 = setTimeout(() => setStep(2), 1200);
    const t3 = setTimeout(() => setStep(3), 1800);
    return () => { clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); };
  }, []);

  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center p-6 backdrop-blur-md animate-in fade-in duration-300" style={{ backgroundColor: darkMode ? 'rgba(15, 23, 42, 0.75)' : 'rgba(255, 255, 255, 0.75)' }}>
      <div className="w-full max-w-sm rounded-lg border shadow-2xl overflow-hidden animate-in zoom-in-95 duration-500" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>
        <div className="p-4 border-b flex items-center gap-3" style={{ borderColor: theme.rule, backgroundColor: darkMode ? '#1E293B' : '#F8FAFC' }}>
          <div className="w-10 h-10 rounded-full flex items-center justify-center bg-blue-100 text-blue-600">
            <User size={20} />
          </div>
          <div>
            <h3 className="font-bold text-sm" style={{ color: theme.ink }}>Agent Console</h3>
            <p className="text-[11px]" style={{ color: theme.inkMuted }}>Handoff Initiated</p>
          </div>
        </div>
        <div className="p-5 flex flex-col gap-4">
          <div className="flex items-center justify-between text-sm">
            <span style={{ color: theme.ink }} className="font-medium">Full Transcript</span>
            {step >= 1 ? <CheckCircle2 size={18} className="text-green-500 animate-in zoom-in" /> : <div className="w-4 h-4 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />}
          </div>
          <div className="flex items-center justify-between text-sm">
            <span style={{ color: theme.ink }} className="font-medium">CRM Context</span>
            {step >= 2 ? <CheckCircle2 size={18} className="text-green-500 animate-in zoom-in" /> : <div className="w-4 h-4 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />}
          </div>
          <div className="flex items-center justify-between text-sm">
            <span style={{ color: theme.ink }} className="font-medium">Intent History</span>
            {step >= 3 ? <CheckCircle2 size={18} className="text-green-500 animate-in zoom-in" /> : <div className="w-4 h-4 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />}
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Screen 1 — Bot Selection Dashboard (PRD Section 9, Screen 1)
// Items 6, 8, 29, 36, 40
// ============================================================================
function IntentPanel({ callState, theme }: { callState: CallState; theme: Theme }) {
  const idle = callState === 'idle' || callState === 'ended';
  const intents = [
    { label: 'High interest', score: 0.86, lead: true },
    { label: 'Needs detail', score: 0.62, lead: false },
    { label: 'Objection — has cards', score: 0.18, lead: false },
    { label: 'Hard rejection', score: 0.04, lead: false },
  ];
  return (
    <div className="flex flex-col gap-1.5">
      {intents.map(i => (
        <div key={i.label} className="flex items-center gap-2 py-1.5 px-2 rounded-md text-[12.5px]">
          <span className="flex-none w-28 truncate" style={{ color: theme.ink }}>{i.label}</span>
          <div className="flex-1 h-[5px] rounded-full overflow-hidden" style={{ backgroundColor: theme.ruleSubtle }}>
            <div className="h-full rounded-full transition-all duration-700" style={{ width: idle ? '0%' : `${i.score * 100}%`, backgroundColor: i.lead ? theme.primary : theme.faint }}></div>
          </div>
          <span className="w-8 text-right text-[11.5px] font-medium" style={{ fontFamily: '"DM Mono", monospace', color: i.lead ? theme.primary : theme.inkMuted }}>
            {idle ? '—' : `.${String(Math.round(i.score * 100)).padStart(2, '0')}`}
          </span>
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// Screen 3 — Active Call Sandbox (PRD Section 9, Screen 3 + Section 5)
// ============================================================================
interface ActiveCallViewProps {
  bot: Bot;
  onBack: () => void;
  onCallCompleted: (call: CallHistory) => void;
  theme: Theme;
  darkMode: boolean;
  live?: boolean;
  token?: string | null;
}

export function ActiveCallView({ bot, onBack, onCallCompleted, theme, darkMode, live = false, token = null }: ActiveCallViewProps) {
  const scenario = SCENARIOS[bot.useCase];
  const customer = MOCK_CUSTOMERS[bot.useCase];
  // True only when this bot is backed by VB_Svc AND we hold a token.
  const liveCall = live && !!token && !!bot.backendId;

  const [callState, setCallState] = useState<CallState>('idle');
  const [transcript, setTranscript] = useState<TranscriptMessageData[]>([]);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [duration, setDuration] = useState<number>(0);
  const [expandedInteraction, setExpandedInteraction] = useState<string | null>(null);
  const [noInputCountdown, setNoInputCountdown] = useState<number | null>(null);
  const [graceCountdown, setGraceCountdown] = useState<number | null>(null);
  const [sessionId, setSessionId] = useState<string>('');
  const [soundEnabled, setSoundEnabled] = useState(false); // Item 52

  const timerRef = useRef<number | null>(null);
  const pendingTimeouts = useRef<number[]>([]);
  const noInputTimerRef = useRef<number | null>(null);
  const graceIntervalRef = useRef<number | null>(null); // Items 15/22: track grace interval
  const rePromptedRef = useRef(false);
  const outcomeRef = useRef<{ label: string; status: CallHistory['status'] }>({ label: 'Completed', status: 'green' });
  const transcriptRef = useRef<TranscriptMessageData[]>([]);
  const durationRef = useRef(0);
  const endedRef = useRef(false);
  const lastFillerRef = useRef<number>(-1);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const eventsEndRef = useRef<HTMLDivElement>(null); // Item 50
  const eventsContainerRef = useRef<HTMLDivElement>(null);
  const firstUserMsgRef = useRef(false); // Track first user message for Items 14, 18
  const callStartTimeRef = useRef(0); // Item 24: wall-clock start time
  const noReconnectRef = useRef(false); // Item 11: user chose no reconnect
  const liveCallRef = useRef<LiveCall | null>(null); // live WS session
  const liveCrmRef = useRef<Record<string, unknown>>({}); // CRM context from session init
  const liveReconnectRef = useRef<(() => void) | null>(null); // re-bind socket within grace window

  // Item 49: sync refs in useEffect instead of render
  useEffect(() => { transcriptRef.current = transcript; }, [transcript]);
  useEffect(() => { durationRef.current = duration; }, [duration]);

  const audioCtxRef = useRef<AudioContext | null>(null);
  // Real AnalyserNode in the playback graph so the waveform reflects the actual
  // bot audio (voice + server-mixed call-floor bed), not a synthetic oscillator.
  const analyserRef = useRef<AnalyserNode | null>(null);
  const masterGainRef = useRef<GainNode | null>(null);
  const audioQueueRef = useRef<AudioBuffer[]>([]);
  const audioPlayingRef = useRef(false);
  const audioSrcRef = useRef<AudioBufferSourceNode | null>(null);

  // Lazily build the playback graph: source -> analyser -> masterGain -> output.
  // masterGain is the mute switch; the analyser always sees audio so the
  // visualizer stays amplitude-reactive even when muted.
  const ensureAudioGraph = (): AudioContext | null => {
    if (audioCtxRef.current) return audioCtxRef.current;
    const Ctor = window.AudioContext || (window as any).webkitAudioContext;
    if (!Ctor) return null;
    const ctx: AudioContext = new Ctor();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 64;
    const gain = ctx.createGain();
    gain.gain.value = soundEnabled ? 1 : 0;
    analyser.connect(gain);
    gain.connect(ctx.destination);
    audioCtxRef.current = ctx;
    analyserRef.current = analyser;
    masterGainRef.current = gain;
    return ctx;
  };

  const playNextAudio = () => {
    const ctx = audioCtxRef.current;
    const analyser = analyserRef.current;
    const buf = audioQueueRef.current.shift();
    if (!buf || !ctx || !analyser) {
      // Queue drained after playing → tell the server the bot truly finished so
      // the no-input timer starts now, not from the send-time estimate.
      if (audioPlayingRef.current) liveCallRef.current?.playbackDone();
      audioPlayingRef.current = false;
      return;
    }
    audioPlayingRef.current = true;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(analyser);
    src.onended = () => { if (audioSrcRef.current === src) audioSrcRef.current = null; playNextAudio(); };
    audioSrcRef.current = src;
    src.start();
  };

  // Decode a base64 WAV chunk from the backend and enqueue it for gapless playback.
  const enqueueAudio = async (audio_b64?: string) => {
    if (!audio_b64) return;
    const ctx = ensureAudioGraph();
    if (!ctx) return;
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch { /* user-gesture gated */ } }
    try {
      const bytes = Uint8Array.from(atob(audio_b64), (c) => c.charCodeAt(0));
      const buffer = await ctx.decodeAudioData(bytes.buffer);
      audioQueueRef.current.push(buffer);
      if (!audioPlayingRef.current) playNextAudio();
    } catch { /* undecodable chunk — skip rather than break the call */ }
  };

  // Stop playback immediately (barge-in TTS_KILL, or call end) and drop the queue.
  const flushPlayback = () => {
    audioQueueRef.current = [];
    if (audioSrcRef.current) {
      try { audioSrcRef.current.onended = null; audioSrcRef.current.stop(); } catch { /* already stopped */ }
      audioSrcRef.current = null;
    }
    audioPlayingRef.current = false;
  };

  // Keep the mute switch in sync; release playback when the call is not active.
  useEffect(() => {
    if (masterGainRef.current) masterGainRef.current.gain.value = soundEnabled ? 1 : 0;
    if (callState === 'idle' || callState === 'ended') flushPlayback();
  }, [soundEnabled, callState]);

  // --- real microphone capture (live mode) ---
  const [micOn, setMicOn] = useState(false);
  const micRef = useRef<MicCapture | null>(null);

  const stopMic = () => {
    micRef.current?.stop();
    micRef.current = null;
    setMicOn(false);
  };

  const toggleMic = async () => {
    if (micOn) { stopMic(); return; }
    if (!liveCall) { logEvent('Microphone is only available in live mode (real VB_Svc).', 'warn'); return; }
    try {
      const cap = new MicCapture({
        onUtterance: (b64, fmt, pcm16) => {
          logEvent(`Mic utterance captured (${fmt}) → streaming to STT`, 'info');
          liveCallRef.current?.sendAudioChunk(b64, fmt, pcm16);
        },
        // Real barge-in: the instant the caller speaks over the bot, cut the bot's
        // audio locally (feels like a real interruption) and tell VB_Svc so it stops
        // streaming the rest of the turn. Echo cancellation keeps the bot's own
        // audio from self-triggering this.
        onSpeechStart: () => {
          if (audioPlayingRef.current) {
            flushPlayback();
            liveCallRef.current?.bargeIn('', 0);
            logEvent('Barge-in — caller spoke over the bot; playback halted', 'info');
          }
        },
        onError: (err) => logEvent(`Mic error: ${err instanceof Error ? err.message : String(err)}`, 'warn'),
      });
      await cap.start();
      micRef.current = cap;
      setMicOn(true);
      logEvent('Microphone armed — speak; VAD endpoints each utterance', 'ok');
    } catch (err) {
      logEvent(`Microphone access denied/unavailable: ${err instanceof Error ? err.message : String(err)}`, 'warn');
      setMicOn(false);
    }
  };

  // Release the mic when the call ends and on unmount.
  useEffect(() => {
    if (callState === 'idle' || callState === 'ended') stopMic();
  }, [callState]);
  useEffect(() => () => stopMic(), []);

  const demoActiveRef = useRef(false);
  const [demoStepTooltip, setDemoStepTooltip] = useState<{ id: string, text: string } | null>(null);

  // Guided auto-demo driven against the real backend (live mode).
  const runGuidedDemoLive = () => {
    demoActiveRef.current = true;
    startCall();
    let time = 2500; // greeting arrives fast over the socket
    schedule(() => { if (demoActiveRef.current) { setDemoStepTooltip({ id: 'chip-1', text: 'Customer raises an objection' }); handleChip(scenario.chips[1] || scenario.chips[0]); } }, time);
    time += 3500;
    schedule(() => { if (demoActiveRef.current) { setDemoStepTooltip({ id: 'barge-in', text: 'Customer interrupts (barge-in)' }); handleBargeIn(); } }, time);
    time += 4000;
    schedule(() => { if (demoActiveRef.current) { setDemoStepTooltip({ id: 'escalate', text: 'Customer asks for a human agent' }); liveCallRef.current?.sendText('Mujhe kisi agent se baat karni hai please'); } }, time);
    schedule(() => { demoActiveRef.current = false; setDemoStepTooltip(null); }, time + 4000);
  };

  // Item 51: Auto-demo orchestrator
  const runGuidedDemo = () => {
    if (liveCall) { runGuidedDemoLive(); return; }
    demoActiveRef.current = true;
    startCall();

    let time = 3500; // Let greeting finish
    
    // Step 1: Normal Response
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'chip-0', text: 'Simulating positive customer response' });
      simulateUserResponse(scenario.chips[0]);
    }, time);
    time += 6000;
    
    // Step 2: Barge In
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'barge-in', text: 'Simulating interruption (Barge-In)' });
      simulateBargeIn();
    }, time);
    time += 6000;
    
    // Step 3: Noise
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'noise', text: 'Simulating background noise (filtered by VAD)' });
      simulateBackgroundNoise();
    }, time);
    time += 4000;
    
    // Step 4: Language Switch
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'lang-switch', text: 'Customer switches language mid-call' });
      simulateLanguageSwitch();
    }, time);
    time += 6000;
    
    // Step 5: Escalation
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'escalate', text: 'Customer requests agent' });
      simulateUserResponse({ 
        label: 'Speak to Agent', 
        userText: 'Mujhe ek human agent se baat karni hai, ye bot se nahi hoga.', 
        botReply: 'Okay, I understand. Please hold while I connect you to an agent who can help you further.', 
        tone: 'empathetic', 
        action: 'ESCALATE' 
      });
    }, time);
    
    schedule(() => {
      demoActiveRef.current = false;
      setDemoStepTooltip(null);
    }, time + 4000);
  };

  // --- low-level helpers ---
  const getSpeechDuration = (text: string) => Math.max(1500, text.length * 60);

  const schedule = (fn: () => void, ms: number) => {
    const t = window.setTimeout(fn, ms);
    pendingTimeouts.current.push(t);
    return t;
  };
  // Items 15/22: clear grace interval too
  const clearAllScheduled = () => {
    pendingTimeouts.current.forEach(t => window.clearTimeout(t));
    pendingTimeouts.current = [];
    if (noInputTimerRef.current) { window.clearInterval(noInputTimerRef.current); noInputTimerRef.current = null; }
    if (graceIntervalRef.current) { window.clearInterval(graceIntervalRef.current); graceIntervalRef.current = null; }
    setNoInputCountdown(null);
    setGraceCountdown(null);
  };

  // Item 49: generate id before setState
  const addMsg = (msg: Omit<TranscriptMessageData, 'id'>) => {
    const id = nextId();
    const timestamp = durationRef.current;
    setTranscript(prev => [...prev, { ...msg, id, timestamp }]);
    // Demo mode only: synthesize bot speech in the browser. In live mode the real
    // bot audio (Orpheus/Magpie + ambient bed) streams from VB_Svc and is played
    // via enqueueAudio(), so browser SpeechSynthesis would double up — skip it.
    if (!liveCall && soundEnabled && msg.kind === 'bot' && typeof window !== 'undefined' && window.speechSynthesis) {
      const utter = new SpeechSynthesisUtterance(msg.text);
      utter.rate = msg.tone === 'firm' ? 0.9 : msg.tone === 'cheerful' ? 1.1 : 1.0;
      utter.pitch = msg.tone === 'empathetic' ? 0.9 : 1.0;
      window.speechSynthesis.speak(utter);
    }
  };

  const logEvent = (label: string, level: SessionEvent['level'] = 'info') => {
    const id = nextId();
    setEvents(prev => [...prev, { id, t: formatTime(durationRef.current), label, level }]);
  };

  // --- transcript autoscroll / call timer ---
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcript, callState]);

  // Item 50: OTel event auto-scroll
  useEffect(() => {
    eventsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  // Item 24: fix duration timer — depend on derived isLive, not callState
  const isLive = callState !== 'idle' && callState !== 'ended';
  useEffect(() => {
    if (isLive) {
      timerRef.current = window.setInterval(() => setDuration(d => d + 1), 1000);
    } else if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    return () => { if (timerRef.current !== null) { window.clearInterval(timerRef.current); timerRef.current = null; } };
  }, [isLive]);

  // Cleanup on unmount — also finalize + close any live socket so navigating
  // away mid-call (e.g. "Exit Sandbox") ends the backend call cleanly instead
  // of leaking the WebSocket and waiting for the 15s grace TTL to expire.
  useEffect(() => () => {
    clearAllScheduled();
    if (timerRef.current) window.clearInterval(timerRef.current);
    if (liveCallRef.current) {
      try { liveCallRef.current.endCall(); } catch { /* socket may be gone */ }
      liveCallRef.current.close();
      liveCallRef.current = null;
    }
  }, []);

  // --- no-input watchdog (PRD: 5s → re-prompt, further 5s → polite close) ---
  // In live mode VB_Svc owns this timer (it re-prompts/closes over the socket),
  // so the simulated watchdog is disabled to avoid double-firing.
  useEffect(() => {
    if (liveCall) return;
    if (callState !== 'listening') {
      if (noInputTimerRef.current) { window.clearInterval(noInputTimerRef.current); noInputTimerRef.current = null; }
      setNoInputCountdown(null);
      return;
    }
    let remaining = 5;
    setNoInputCountdown(remaining);
    noInputTimerRef.current = window.setInterval(() => {
      remaining -= 1;
      setNoInputCountdown(remaining);
      if (remaining <= 0) {
        window.clearInterval(noInputTimerRef.current!);
        noInputTimerRef.current = null;
        handleNoInput();
      }
    }, 1000);
    return () => { if (noInputTimerRef.current) { window.clearInterval(noInputTimerRef.current); noInputTimerRef.current = null; } };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callState]);

  const handleNoInput = () => {
    if (!rePromptedRef.current) {
      rePromptedRef.current = true;
      logEvent('NO_INPUT_1 — 5s silence, re-prompting', 'warn');
      addMsg({ kind: 'meta', text: 'No speech detected for 5s — NO_INPUT_1 logged, timer reset' });
      setCallState('bot_speaking');
      schedule(() => {
        addMsg({ kind: 'bot', text: scenario.rePrompt, tone: 'neutral', latency: randLatency() });
        schedule(() => setCallState('listening'), getSpeechDuration(scenario.rePrompt));
      }, 600);
    } else {
      // Item 4: transition to bot_speaking so status/chips reflect correctly
      logEvent('NO_INPUT_2 — closing politely, outcome customer_unresponsive', 'warn');
      setCallState('bot_speaking');
      addMsg({ kind: 'bot', text: scenario.politeClose, tone: 'empathetic', latency: randLatency() });
      addMsg({ kind: 'meta', text: 'Outcome: customer_unresponsive · CRM flagged for retry scheduling' });
      outcomeRef.current = { label: 'Customer Unresponsive', status: 'gray' };
      schedule(() => endCall(), 1800);
    }
  };

  // Backend outcome tag → call-history label + status colour.
  const BACKEND_OUTCOME: Record<string, { label: string; status: CallHistory['status'] }> = {
    escalated: { label: 'Escalated to Agent', status: 'orange' },
    converted: { label: 'Completed — Interested', status: 'green' },
    declined: { label: 'Declined', status: 'red' },
    customer_unresponsive: { label: 'Customer Unresponsive', status: 'gray' },
    call_dropped: { label: 'Call Dropped', status: 'gray' },
    unsupported_language: { label: 'Unsupported Language', status: 'orange' },
    in_progress: { label: 'Ended by Operator', status: 'gray' },
  };

  // --- live session lifecycle (real VB_Svc over WebSocket) ---
  const startLiveCall = async () => {
    clearAllScheduled();
    endedRef.current = false;
    rePromptedRef.current = false;
    firstUserMsgRef.current = false;
    outcomeRef.current = { label: 'Completed', status: 'green' };
    callStartTimeRef.current = Date.now();
    setTranscript([]);
    setEvents([]);
    setDuration(0);
    setCallState('connecting');
    logEvent('WebSocket handshake — short-lived JWT presented, upgrading…');

    try {
      const result = await apiStartSession(token!, bot.backendId!, bot.backendCustomerId ?? null);
      liveCrmRef.current = result.crm_context || {};
      setSessionId(result.session_id);
      logEvent(`Session ${result.session_id.slice(0, 8)} created · CRM record loaded · use_case=${result.use_case}`, 'ok');

      const handlers = {
        onOpen: () => logEvent('WebSocket upgraded — JWT accepted by VB_Svc', 'ok'),
        onCallState: (status) => setCallState(status as CallState),
        onTranscript: (speaker, text, d) =>
          addMsg({ kind: speaker === 'bot' ? 'bot' : 'user', text, tone: d.tone as Tone | undefined }),
        onAudio: (d) => { void enqueueAudio(d.audio_b64 as string | undefined); },
        onFiller: (text) => {
          addMsg({ kind: 'filler', text });
          logEvent('NLP first token >300ms — pre-rendered filler injected (latency masking)', 'warn');
        },
        onTtsKill: (reason) => {
          flushPlayback();  // halt in-flight bot audio immediately (barge-in)
          setTranscript(prev => {
            const copy = [...prev];
            for (let i = copy.length - 1; i >= 0; i--) {
              if (copy[i].kind === 'bot') { copy[i] = { ...copy[i], interrupted: true }; break; }
            }
            return copy;
          });
          addMsg({ kind: 'meta', text: `Gate 2: ${reason} → TTS_KILL issued · playback buffer flushed` });
          logEvent('BARGE_IN confirmed — TTS halted (<150ms target)', 'warn');
        },
        onReconnected: (text) => {
          addMsg({ kind: 'meta', text: 'Reconnected with same Session ID — state restored from Redis' });
          logEvent('Reconnect inside grace window — session re-bound', 'ok');
          if (text) addMsg({ kind: 'bot', text, tone: 'neutral' });
        },
        onError: (code, message) => logEvent(`Server error ${code}: ${message}`, 'warn'),
        onEnded: (outcome) => {
          outcomeRef.current = BACKEND_OUTCOME[outcome] || { label: 'Completed', status: 'green' };
          logEvent(`Outcome=${outcome} · transcript + metadata persisted to PostgreSQL`, 'ok');
          endCall();
        },
        onSocketClose: () => { if (!endedRef.current) logEvent('Socket closed', 'info'); },
      };
      const makeCall = () => {
        const call = new LiveCall(result.session_id, result.ws_token, handlers);
        liveCallRef.current = call;
        call.connect();
      };
      // Reconnect path for the grace-TTL demo: re-bind the same session id.
      liveReconnectRef.current = makeCall;
      makeCall();
    } catch (err) {
      const msg = err instanceof KoelApiError ? `${err.code}: ${err.message}` : 'connection failed';
      addMsg({ kind: 'meta', text: `Session refused — ${msg}` });
      logEvent(`Session start failed — ${msg}`, 'warn');
      outcomeRef.current = { label: 'Not Connected', status: 'red' };
      endCall();
    }
  };

  // --- session lifecycle ---
  const startCall = () => {
    if (liveCall) { void startLiveCall(); return; }
    clearAllScheduled();
    endedRef.current = false;
    rePromptedRef.current = false;
    lastFillerRef.current = -1;
    firstUserMsgRef.current = false;
    noReconnectRef.current = false;
    outcomeRef.current = { label: 'Completed', status: 'green' };
    callStartTimeRef.current = Date.now();
    const sid = `SES-${Math.random().toString(36).slice(2, 8).toUpperCase()}`;
    setSessionId(sid);
    setTranscript([]);
    setEvents([]);
    setDuration(0);
    setCallState('connecting');
    logEvent('WebSocket handshake — JWT presented, upgrading…');
    // Item 19: pre-dial compliance checks
    logEvent(`Pre-dial checks: ${new Date().toLocaleTimeString('en-IN', { hour12: false })} IST within RBI permitted window (09:00–21:00) · Number not on TRAI DND registry`, 'ok');

    schedule(() => {
      logEvent(`Session ${sid} bound · Redis state created · CRM record loaded`, 'ok');
      logEvent('Ambient audio layer mixed under TTS output');
      setCallState('bot_speaking');
      schedule(() => {
        // Item 27: compute total from components
        const ttsVal = 170 + Math.floor(Math.random() * 40);
        addMsg({ kind: 'bot', text: scenario.greeting, tone: scenario.greetingTone, latency: { stt: 0, nlp: 0, tts: ttsVal, total: ttsVal } });
        logEvent('Greeting synthesised from CRM-personalised template');
        schedule(() => setCallState('listening'), getSpeechDuration(scenario.greeting));
      }, 1100);
    }, 1200);
  };

  const endCall = () => {
    if (endedRef.current) return;
    endedRef.current = true;
    demoActiveRef.current = false;
    setDemoStepTooltip(null);
    clearAllScheduled();
    if (liveCallRef.current) { liveCallRef.current.close(); liveCallRef.current = null; }
    if (soundEnabled && window.speechSynthesis) window.speechSynthesis.cancel();
    setCallState('ended');
    if (!liveCall) logEvent('Session flushed from Redis · transcript + outcome persisted to PostgreSQL', 'ok');

    const finalTranscript = transcriptRef.current;
    const dur = durationRef.current;
    let { label: outcome, status } = outcomeRef.current;
    // Item 25: fix outcome for manual hang-ups
    if (outcome === 'Completed') {
      const userMsgs = finalTranscript.filter(m => m.kind === 'user').length;
      if (userMsgs === 0) { outcome = 'Customer Hung Up'; status = 'red'; }
      else { outcome = 'Ended by Operator'; status = 'gray'; }
    }

    const liveNow = new Date();
    const lats = finalTranscript.filter(m => m.kind === 'bot' && m.latency).map(m => m.latency!.total);
    const avgLat = lats.length ? Math.round(lats.reduce((a, b) => a + b, 0) / lats.length) : 0;

    onCallCompleted({
      // Item 46: unique call IDs
      id: `CALL-${Date.now().toString(36).toUpperCase()}`,
      bot: bot.name,
      language: bot.language,
      durationStr: `${Math.floor(dur / 60)}m ${(dur % 60).toString().padStart(2, '0')}s`,
      durationSec: dur,
      outcome,
      dateStr: liveNow.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }) + ', ' + liveNow.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false }),
      timestamp: liveNow.getTime(),
      status,
      customer: customer.name,
      customerId: customer.customerId, // Item 16
      phone: customer.phone,
      avgLatencyMs: avgLat,
      transcript: finalTranscript,
      events: [...events], // Item 35
    });
  };

  // --- core turn engine ---
  const runBotTurn = (chip: ChipDef) => {
    setCallState('processing');
    logEvent('Final transcript → NLP_svc (Groq) · intent classification + RAG');

    const lat = randLatency(chip.nlpSlow);

    if (chip.nlpSlow) {
      schedule(() => {
        const pool = FILLER_POOLS[bot.language] || FILLER_POOLS.English;
        const idx = (lastFillerRef.current + 1) % pool.length;
        lastFillerRef.current = idx;
        addMsg({ kind: 'filler', text: pool[idx] });
        logEvent(`NLP first token >300ms — filler clip ${idx + 1}/${pool.length} (${bot.language}) injected, first byte 84ms`, 'warn');
      }, 450);
    }

    schedule(() => {
      if (chip.ragContext) addMsg({ kind: 'meta', text: chip.ragContext });
      // Item 17: IRDAI consent capture
      if (chip.consentCapture) {
        addMsg({ kind: 'meta', text: `${chip.consentCapture} · ${new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}` });
        logEvent('IRDAI verbal consent recorded with timestamp', 'ok');
      }
      setCallState(chip.action === 'ESCALATE' ? 'escalating' : 'bot_speaking');
      addMsg({ kind: 'bot', text: chip.botReply, tone: chip.tone, latency: lat });
      logEvent(`Turn complete — STT ${lat.stt}ms · NLP ${lat.nlp}ms · TTS ${lat.tts}ms · total ${lat.total}ms`, lat.total <= 300 ? 'ok' : 'warn');

      if (chip.outcome) outcomeRef.current = chip.outcome;

      if (chip.action === 'ESCALATE') {
        // Item 12: mute ambient on escalation
        logEvent('Ambient audio bed muted for agent handoff', 'info');
        schedule(() => {
          addMsg({ kind: 'meta', text: 'Escalation packet dispatched: full transcript + CRM context + intent history → agent queue' });
          logEvent('Escalation handoff — 100% context, no repeat authentication', 'ok');
          schedule(() => endCall(), 1600);
        }, 1800);
      } else if (chip.action === 'END_POLITE') {
        schedule(() => endCall(), getSpeechDuration(chip.botReply));
      } else {
        schedule(() => setCallState('listening'), getSpeechDuration(chip.botReply));
      }
    }, chip.nlpSlow ? 2100 : 1100);
  };

  const simulateUserResponse = (chip: ChipDef) => {
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: chip.userText });

    // Item 14: first-utterance language detection
    if (!firstUserMsgRef.current) {
      firstUserMsgRef.current = true;
      const langCode = LANG_CODES[bot.language] || 'en';
      logEvent(`STT language_id=${langCode} (${bot.language}) · confidence 0.96 — language locked for session`, 'ok');
      // Item 18: DPDP consent at call open
      addMsg({ kind: 'meta', text: `DPDP consent captured at call open · ${new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}` });
      logEvent('DPDP consent captured — customer affirmed at call open', 'ok');
    }

    logEvent('Customer audio streaming → STT_svc (Whisper) · partial transcripts');
    schedule(() => runBotTurn(chip), 900);
  };

  // --- resilience edge cases (PRD Section 5) ---
  const simulateBackchannel = () => {
    if (callState !== 'bot_speaking') return;
    addMsg({ kind: 'meta', text: 'Gate 1: VAD flagged BARGE_IN_CANDIDATE → Gate 2: "Hmm" matched backchannel dictionary — ignored, TTS continues' });
    logEvent('Backchannel "Hmm" — dual-gate ignored, no TTS interruption', 'ok');
  };

  const simulateBargeIn = () => {
    if (callState !== 'bot_speaking') return;
    clearAllScheduled();
    // Item 23: reset outcomeRef when barge-in cancels terminal close
    outcomeRef.current = { label: 'Completed', status: 'green' };
    setTranscript(prev => {
      const copy = [...prev];
      for (let i = copy.length - 1; i >= 0; i--) {
        if (copy[i].kind === 'bot') { copy[i] = { ...copy[i], interrupted: true }; break; }
      }
      return copy;
    });
    // Item 32: show Gate 1 before Gate 2
    addMsg({ kind: 'meta', text: 'Gate 1: VAD flagged BARGE_IN_CANDIDATE — forwarding to Gate 2' });
    addMsg({ kind: 'meta', text: 'Gate 2: sustained non-backchannel speech → TTS_KILL issued · playback buffer flushed in 132ms' });
    logEvent('BARGE_IN confirmed — TTS halted in 132ms (<150ms target)', 'warn');
    if (soundEnabled && window.speechSynthesis) window.speechSynthesis.cancel();
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: scenario.bargeInUserText });
    schedule(() => runBotTurn({
      label: '', userText: '', botReply: scenario.bargeInReply, tone: 'empathetic',
      ragContext: 'Context note: previous bot turn marked [interrupted] — LLM knows what the customer actually heard'
    }), 900);
  };

  const simulateUnsupportedLanguage = () => {
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: 'मला हे समजत नाही, मराठीत बोला ना…' });
    logEvent('STT language_id=mr (Marathi) · confidence 0.94 — unsupported', 'warn');
    schedule(() => {
      setCallState('processing');
      schedule(() => {
        addMsg({ kind: 'meta', text: 'Graceful language fallback — no hallucinated translation. Session tagged unsupported_language' });
        setCallState('escalating');
        logEvent('Ambient audio bed muted for agent handoff', 'info');
        addMsg({ kind: 'bot', tone: 'empathetic', latency: randLatency(), text: 'Maaf kijiye, main keval Hindi ya English samajh sakti hoon. Main aapko ek agent se connect karti hoon.' });
        outcomeRef.current = { label: 'Unsupported Language', status: 'orange' };
        schedule(() => {
          addMsg({ kind: 'meta', text: 'Escalated to multilingual agent queue with full context' });
          schedule(() => endCall(), 1400);
        }, 1800);
      }, 900);
    }, 800);
  };

  // Item 2: mid-call language switching
  const simulateLanguageSwitch = () => {
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: 'Continue in English please.' });
    logEvent('STT language_id change: hi-Latn → en · confidence 0.93 — session language updated', 'warn');
    addMsg({ kind: 'meta', text: 'Mid-call language switch detected: Hinglish → English · session language updated · TTS voice profile adjusted' });
    schedule(() => {
      setCallState('processing');
      schedule(() => {
        setCallState('bot_speaking');
        const replyText = 'Sure, switching to English. So as I was saying, this offer is specifically tailored to your spending profile. Would you like to hear the details?';
        addMsg({ kind: 'bot', tone: 'cheerful', latency: randLatency(), text: replyText });
        logEvent('Language switch complete — TTS now rendering in English', 'ok');
        schedule(() => setCallState('listening'), getSpeechDuration(replyText));
      }, 1100);
    }, 900);
  };

  // Item 10: background noise doesn't reset no-input timer
  const simulateBackgroundNoise = () => {
    if (callState !== 'listening') return;
    addMsg({ kind: 'meta', text: 'VAD: non-speech energy detected (background noise / cough) — no STT cycle triggered, no-input timer NOT reset' });
    logEvent('Non-speech audio filtered by VAD — no-input countdown continues', 'info');
  };

  const simulateNetworkDrop = () => {
    if (callState === 'idle' || callState === 'ended' || callState === 'reconnecting' || callState === 'connecting') return;
    clearAllScheduled();
    noReconnectRef.current = false;
    setCallState('reconnecting');
    addMsg({ kind: 'meta', text: 'WebSocket dropped (network handoff) · TTS paused · Redis session held on 15s grace TTL' });
    logEvent('Socket disconnect — session frozen, 15s grace TTL armed', 'warn');
    if (soundEnabled && window.speechSynthesis) window.speechSynthesis.cancel();

    let remaining = 15;
    setGraceCountdown(remaining);
    graceIntervalRef.current = window.setInterval(() => {
      remaining -= 1;
      setGraceCountdown(remaining);
      // Item 11: if no-reconnect was triggered, let TTL expire
      if (remaining <= 0) {
        if (graceIntervalRef.current) { window.clearInterval(graceIntervalRef.current); graceIntervalRef.current = null; }
        setGraceCountdown(null);
        addMsg({ kind: 'meta', text: 'No reconnect within 15s grace window — session closed, outcome call_dropped' });
        logEvent('Grace TTL expired — no reconnect, session terminated as call_dropped', 'warn');
        outcomeRef.current = { label: 'Call Dropped', status: 'gray' };
        endCall();
        return;
      }
    }, 1000);

    // Client reconnects after ~4s unless user blocked it
    schedule(() => {
      if (noReconnectRef.current) return; // user chose not to reconnect
      if (graceIntervalRef.current) { window.clearInterval(graceIntervalRef.current); graceIntervalRef.current = null; }
      setGraceCountdown(null);
      addMsg({ kind: 'meta', text: `Reconnected with same Session ID (${sessionId}) — RECONNECTED span emitted, state restored from Redis` });
      logEvent('Reconnect inside grace window — session re-bound to new socket', 'ok');
      setCallState('bot_speaking');
      schedule(() => {
        const reconnectText = 'Sorry, lagta hai line cut ho gayi thi — am I audible? Toh jaisa main keh rahi thi…';
        addMsg({ kind: 'bot', tone: 'neutral', latency: randLatency(), text: reconnectText });
        schedule(() => setCallState('listening'), getSpeechDuration(reconnectText));
      }, 800);
    }, 4000);
  };

  // Item 11: allow grace TTL to expire
  const simulateNoReconnect = () => {
    if (callState !== 'reconnecting') return;
    noReconnectRef.current = true;
    // Cancel the scheduled reconnect
    pendingTimeouts.current.forEach(t => window.clearTimeout(t));
    pendingTimeouts.current = [];
    addMsg({ kind: 'meta', text: 'Reconnect blocked — waiting for 15s grace TTL to expire…' });
    logEvent('No-reconnect path active — grace window counting down to expiry', 'warn');
  };

  // --- live-aware interaction handlers (route to VB_Svc when live, else simulate) ---
  const beLang = bot.language === 'Hindi' ? 'hi' : bot.language === 'English' ? 'en' : 'hinglish';

  const handleChip = (chip: ChipDef) => {
    if (!liveCall) { simulateUserResponse(chip); return; }
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    liveCallRef.current?.sendText(chip.userText); // backend echoes the transcript
  };

  const handleBargeIn = () => {
    if (!liveCall) { simulateBargeIn(); return; }
    // Gate-1 candidate: sustained, non-backchannel speech → backend issues TTS_KILL.
    liveCallRef.current?.bargeIn('ruko ruko, ek sawaal hai', 1100, beLang);
    schedule(() => liveCallRef.current?.sendText('Ruko, ek sawaal hai'), 700);
  };

  const handleBackchannel = () => {
    if (!liveCall) { simulateBackchannel(); return; }
    // Short backchannel → Gate 2 ignores it (no TTS_KILL expected).
    liveCallRef.current?.bargeIn('achha', 180, beLang);
    logEvent('Backchannel sent — expecting Gate 2 to ignore (no interruption)', 'info');
  };

  const handleUnsupported = () => {
    if (!liveCall) { simulateUnsupportedLanguage(); return; }
    if (callState !== 'listening') return;
    // Marathi greeting token → STT stub flags language_id=other → fallback path.
    liveCallRef.current?.sendText('नमस्कार, मला हिंदी येत नाही');
  };

  const handleLanguageSwitch = () => {
    if (!liveCall) { simulateLanguageSwitch(); return; }
    if (callState !== 'listening') return;
    liveCallRef.current?.sendText('Please continue in English from now on');
  };

  const handleBackgroundNoise = () => {
    if (!liveCall) { simulateBackgroundNoise(); return; }
    // Pure noise never crosses client-side VAD, so nothing is sent to the server.
    addMsg({ kind: 'meta', text: 'VAD: non-speech energy — filtered client-side, no frame sent to VB_Svc' });
    logEvent('Non-speech audio filtered by VAD — no-input timer NOT reset', 'info');
  };

  const handleNetworkDrop = () => {
    if (!liveCall) { simulateNetworkDrop(); return; }
    if (callState === 'idle' || callState === 'ended' || callState === 'connecting' || callState === 'reconnecting') return;
    setCallState('reconnecting');
    addMsg({ kind: 'meta', text: 'WebSocket dropped · VB_Svc holds the session on a 15s Redis grace TTL' });
    logEvent('Socket closed — backend grace TTL armed (15s)', 'warn');
    liveCallRef.current?.close();
    liveCallRef.current = null;
    // Re-bind the same Session ID inside the grace window (real resume).
    schedule(() => { if (!endedRef.current) liveReconnectRef.current?.(); }, 3000);
  };

  return (
    <div className="flex flex-col bg-white dark:bg-gray-900 animate-in fade-in duration-300" style={{ height: 'calc(100vh - 64px)', backgroundColor: theme.surfaceCard }}>
      {/* Top Bar */}
      <div className="flex items-center justify-between px-4 md:px-6 py-3 border-b z-10 shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surface }}>
        <div className="flex items-center gap-4">
          <button onClick={onBack} className="flex items-center px-3 py-1.5 rounded-lg shadow-sm text-sm font-medium transition-all border focus-visible:ring-2" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.inkMuted }}>
            <ChevronRight className="rotate-180 mr-1" size={16} /> Exit Sandbox
          </button>
          <div className="h-6 w-px" style={{ backgroundColor: theme.rule }}></div>
          <div>
            <h2 className="font-bold text-lg" style={{ color: theme.ink }}>{bot.name}</h2>
            <p className="text-xs font-medium tracking-wide" style={{ color: theme.inkMuted }}>
              {bot.useCase} • {bot.language}{sessionId && isLive ? <> • <span className="font-mono">{sessionId}</span></> : ''}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-4 md:gap-6">
          {/* Item 52: sound toggle */}
          <button onClick={() => setSoundEnabled(!soundEnabled)} className="hidden md:flex items-center gap-1.5 px-2 py-1 rounded text-xs font-bold transition-colors" style={{ color: soundEnabled ? theme.green : theme.inkMuted }} aria-label={soundEnabled ? 'Disable audio' : 'Enable audio'} title={soundEnabled ? 'Audio On (Speech Synthesis)' : 'Audio Off'}>
            <Volume2 size={14} /> {soundEnabled ? 'Audio On' : 'Audio Off'}
          </button>
          <div className="flex flex-col items-end">
            <span className="text-[10px] font-bold uppercase tracking-widest mb-0.5" style={{ color: theme.inkMuted }} role="status" aria-live="polite">Session Status</span>
            {callState === 'idle' && <span className="font-bold px-2.5 py-0.5 rounded text-sm" style={{ backgroundColor: darkMode ? '#374151' : '#E5E7EB', color: theme.inkMuted }}>Ready</span>}
            {callState === 'connecting' && <span className="text-yellow-600 dark:text-yellow-400 font-bold animate-pulse bg-yellow-50 dark:bg-yellow-900/30 px-2.5 py-0.5 rounded text-sm border border-yellow-200 dark:border-yellow-800">Connecting…</span>}
            {callState === 'reconnecting' && (
              <span className="text-orange-600 dark:text-orange-400 font-bold bg-orange-50 dark:bg-orange-900/30 border border-orange-200 dark:border-orange-800 px-2.5 py-0.5 rounded text-sm flex items-center gap-1.5 animate-pulse">
                <WifiOff size={13} /> Reconnecting{graceCountdown !== null ? ` · TTL ${graceCountdown}s` : ''}
              </span>
            )}
            {callState === 'escalating' && (
              <span className="font-bold px-2.5 py-0.5 rounded text-sm flex items-center gap-1.5" style={{ color: theme.accent, backgroundColor: darkMode ? '#7C2D12' + '30' : '#FFF0EB', borderColor: darkMode ? '#9A3412' : '#F5C0AD' }}>
                <Zap size={13} className="animate-pulse" /> Escalating • {formatTime(duration)}
              </span>
            )}
            {/* Item 13: Processing as distinct badge */}
            {callState === 'processing' && (
              <span className="text-yellow-700 dark:text-yellow-400 font-bold bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 px-2.5 py-0.5 rounded text-sm flex items-center gap-1.5">
                <Zap size={13} className="animate-pulse" /> Processing • {formatTime(duration)}
              </span>
            )}
            {(callState === 'bot_speaking' || callState === 'listening' || callState === 'user_speaking') && (
              <span className="text-green-700 dark:text-green-400 font-bold flex items-center gap-2 bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 px-2.5 py-0.5 rounded text-sm">
                <span className="w-2 h-2 rounded-full bg-green-500 animate-ping"></span>Live • {formatTime(duration)}
              </span>
            )}
            {callState === 'ended' && <span className="text-red-600 dark:text-red-400 font-bold bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 px-2.5 py-0.5 rounded text-sm">Ended • {formatTime(duration)}</span>}
          </div>
          {callState === 'idle' || callState === 'ended' ? (
            <button onClick={startCall} className="px-4 md:px-6 py-2.5 rounded-lg text-white font-bold flex items-center gap-2 shadow-md transition-transform hover:scale-105 focus-visible:ring-2" style={{ backgroundColor: theme.primary }}>
              <PhoneCall size={18} /> {callState === 'ended' ? 'Restart' : 'Start Call'}
            </button>
          ) : (
            <button onClick={endCall} className="px-4 md:px-6 py-2.5 rounded-lg bg-red-600 hover:bg-red-700 text-white font-bold flex items-center gap-2 shadow-md transition-transform hover:scale-105 focus-visible:ring-2">
              <Square size={18} fill="currentColor" /> End Call
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Left sidebar: CRM, past interactions, guardrails, OTel */}
        <div className="hidden lg:flex w-[330px] border-r flex-col shrink-0 overflow-y-auto" style={{ borderColor: theme.rule, backgroundColor: theme.sidebarBg }}>
          {/* CRM Panel */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider mb-3" style={{ color: theme.inkMuted }}>
              <Database size={14} /> CRM Record · Dummy CRM
            </div>
            <div className="rounded-lg p-4 shadow-sm border" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
              <div className="flex items-center gap-3 mb-4 pb-4 border-b" style={{ borderColor: darkMode ? '#1F2937' : '#F3F4F6' }}>
                <div className="w-10 h-10 rounded-full flex items-center justify-center font-bold text-lg" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>
                  {customer.name.charAt(0)}
                </div>
                <div>
                  <h3 className="font-bold leading-tight" style={{ color: theme.ink }}>{customer.name}</h3>
                  <p className="text-[11px] font-mono mt-0.5" style={{ color: theme.inkMuted }}>{customer.customerId} · {customer.phone}</p>
                </div>
              </div>
              <div className="space-y-3 text-sm">
                <div className="flex justify-between items-center"><span className="text-xs" style={{ color: theme.inkMuted }}>Segment</span><span className="font-semibold" style={{ color: theme.ink }}>{customer.segment}</span></div>
                {customer.fields.map(f => (
                  <div key={f.label} className="flex justify-between items-center gap-2">
                    <span className="text-xs" style={{ color: theme.inkMuted }}>{f.label}</span>
                    {f.highlight ? (
                      <span className={`font-semibold px-2 py-0.5 rounded-md text-[11px] uppercase tracking-wide ${f.highlight === 'green' ? 'text-green-700 dark:text-green-400 bg-green-100 dark:bg-green-900/30' : 'text-orange-700 dark:text-orange-400 bg-orange-100 dark:bg-orange-900/30'}`}>{f.value}</span>
                    ) : (
                      <span className="font-semibold text-right" style={{ color: theme.ink }}>{f.value}</span>
                    )}
                  </div>
                ))}
              </div>
              <p className="text-[10px] mt-4 pt-3 border-t leading-relaxed" style={{ color: theme.inkMuted, borderColor: darkMode ? '#1F2937' : '#F3F4F6' }}>
                Only use-case-relevant fields loaded into session (DPDP data minimisation). PII masked in logs.
              </p>
            </div>
          </div>

          {/* Intent Classification — live */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                <Activity size={14} /> Intent Classification
              </div>
              <span className="text-[9px] font-medium" style={{ fontFamily: '"DM Mono", monospace', color: theme.faint }}>live</span>
            </div>
            <IntentPanel callState={callState} theme={theme} />
          </div>

          {/* Escalation Packet — shown during escalation */}
          {callState === 'escalating' && (
            <div className="p-5 border-b animate-in fade-in" style={{ borderColor: theme.rule }}>
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                  <Zap size={14} /> Escalation Packet
                </div>
                <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium" style={{ backgroundColor: theme.accentLight, color: '#B23A14' }}>Preparing</span>
              </div>
              <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
                {[
                  ['Target queue', 'Sales specialist'],
                  ['Transcript', 'Attached'],
                  ['CRM context', 'Attached'],
                  ['Re-authentication', 'Not required'],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between items-center px-4 py-2.5 border-b last:border-b-0 text-[13px]" style={{ borderColor: theme.ruleSubtle }}>
                    <span style={{ color: theme.inkMuted }}>{k}</span>
                    <span className="font-medium" style={{ color: v === 'Attached' ? theme.green : theme.ink }}>{v}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Past Interactions */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                <MessageSquare size={14} /> Past Interactions
              </div>
              <span className="text-[10px] px-2 py-0.5 rounded-full font-bold" style={{ backgroundColor: darkMode ? '#374151' : '#E5E7EB', color: theme.inkMuted }}>{PAST_INTERACTIONS.length} Calls</span>
            </div>
            <div className="flex flex-col gap-2">
              {PAST_INTERACTIONS.map(interaction => (
                <div key={interaction.id} className="border rounded-lg overflow-hidden shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
                  <button onClick={() => setExpandedInteraction(expandedInteraction === interaction.id ? null : interaction.id)} className="w-full text-left p-3 flex justify-between items-center transition-colors hover:opacity-80">
                    <div>
                      <p className="font-semibold text-sm leading-tight" style={{ color: theme.ink }}>{interaction.topic}</p>
                      <p className="text-[11px] mt-1" style={{ color: theme.inkMuted }}>{interaction.date} • {interaction.duration}</p>
                    </div>
                    <ChevronDown size={16} className={`transition-transform duration-200 ${expandedInteraction === interaction.id ? 'rotate-180' : ''}`} style={{ color: theme.inkMuted }} />
                  </button>
                  {expandedInteraction === interaction.id && (
                    <div className="p-3 border-t flex flex-col gap-3" style={{ borderColor: darkMode ? '#1F2937' : '#F3F4F6', backgroundColor: theme.sidebarBg }}>
                      {interaction.transcript.map((m, idx) => (
                        <div key={idx} className="flex gap-2 items-start">
                          <div className={`w-5 h-5 rounded-full flex-shrink-0 flex items-center justify-center text-[9px] font-bold text-white mt-0.5 shadow-sm ${m.speaker === 'bot' ? 'bg-[#053BEE]' : 'bg-gray-400'}`}>
                            {m.speaker === 'bot' ? 'K' : 'U'}
                          </div>
                          <p className="text-xs leading-relaxed" style={{ color: m.speaker === 'bot' ? theme.ink : theme.inkMuted }}>{m.text}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Live guardrails */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider mb-3" style={{ color: theme.inkMuted }}>
              <ShieldAlert size={14} /> Active Guardrails
            </div>
            <div className="flex flex-col gap-2">
              {/* Item 12: mute ambient during escalation */}
              <GuardrailItem active={isLive && callState !== 'escalating'} label={callState === 'escalating' ? 'Ambient Audio Layer (MUTED)' : 'Ambient Audio Layer'} theme={theme} />
              <GuardrailItem active={callState === 'bot_speaking'} label="Dual-Gate Barge-In Engine" pulsing={callState === 'bot_speaking'} theme={theme} />
              <GuardrailItem active={callState === 'listening'} label={`No-Input Watchdog${noInputCountdown !== null ? ` (${noInputCountdown}s)` : ''}`} pulsing={callState === 'listening'} theme={theme} />
              <GuardrailItem active={callState === 'processing'} label="pgvector RAG Retrieval" pulsing={callState === 'processing'} theme={theme} />
              <GuardrailItem active={callState === 'reconnecting'} label="15s Session Recovery TTL" pulsing={callState === 'reconnecting'} theme={theme} />
              <GuardrailItem active={isLive} label="Language ID Confidence Check" theme={theme} />
              <GuardrailItem active={isLive} label="Tone-Tagged TTS Prosody" theme={theme} />
            </div>
          </div>

          {/* OTel event trace */}
          <div className="p-5 flex-1">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                <Activity size={14} /> Session Events · OTel Trace
              </div>
              {/* Item 43: Kibana link */}
              <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded" style={{ color: theme.primary, backgroundColor: theme.primarySubtle }}>View in Kibana →</span>
            </div>
            <div ref={eventsContainerRef} className="rounded-lg p-3 font-mono text-[10px] leading-relaxed max-h-56 overflow-y-auto flex flex-col gap-1" style={{ backgroundColor: theme.otelBg }}>
              {events.length === 0 && <span className="text-gray-500">— spans will appear when the session starts —</span>}
              {events.map(ev => (
                <div key={ev.id} className="flex gap-2">
                  <span className="text-gray-500 shrink-0">[{ev.t}]</span>
                  <span className={ev.level === 'ok' ? 'text-green-400' : ev.level === 'warn' ? 'text-yellow-400' : 'text-blue-300'}>{ev.label}</span>
                </div>
              ))}
              <div ref={eventsEndRef} />
            </div>
          </div>
        </div>

        {/* Center: transcript + visualizer + simulation deck */}
        <div className="flex-1 flex flex-col relative min-w-0" style={{ backgroundColor: theme.transcriptBg }}>
          <div className="flex-1 p-4 md:p-8 overflow-y-auto flex flex-col gap-5 pb-72 relative" aria-live="polite">
            {transcript.length === 0 && callState === 'idle' && (
              <div className="absolute inset-0 flex flex-col items-center justify-center p-8" style={{ color: theme.inkMuted }}>
                <PhoneCall size={64} className="mb-6 opacity-20" style={{ color: theme.primary }} />
                <p className="text-xl font-bold mb-2" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>Sandbox Ready</p>
                <p className="text-sm text-center max-w-sm mb-6">Click 'Start Call' to establish the authenticated WebSocket session and stream audio through the Whisper → Groq Llama → Orpheus pipeline.</p>
                <button onClick={runGuidedDemo} className="px-6 py-2.5 rounded-full font-bold text-sm text-white shadow-lg hover:shadow-xl transition-all hover:-translate-y-0.5" style={{ background: `linear-gradient(135deg, ${theme.primary}, ${theme.primaryLight})` }}>
                  ▶ Run Guided Demo
                </button>
              </div>
            )}

            {transcript.map(msg => <TranscriptMessage key={msg.id} msg={msg} theme={theme} />)}

            {(callState === 'bot_speaking' || callState === 'processing' || callState === 'user_speaking' || callState === 'reconnecting' || callState === 'escalating') && (
              <div className="flex gap-2 items-center text-xs font-semibold uppercase tracking-wider p-2 px-4 backdrop-blur-sm rounded-full self-center border shadow-sm" style={{ backgroundColor: darkMode ? 'rgba(17,24,39,0.6)' : 'rgba(255,255,255,0.6)', borderColor: theme.rule, color: theme.inkMuted }}>
                {callState === 'processing' && <><Zap size={14} className="text-yellow-500 animate-pulse" /> Orchestrator routing to NLP_svc…</>}
                {callState === 'bot_speaking' && <><Activity size={14} className="animate-pulse" style={{ color: theme.primary }} /> TTS_svc streaming audio…</>}
                {callState === 'user_speaking' && <><Mic size={14} className="text-green-600 animate-pulse" /> STT_svc transcribing…</>}
                {callState === 'reconnecting' && <><WifiOff size={14} className="text-orange-500 animate-pulse" /> Session frozen — awaiting reconnect…</>}
                {callState === 'escalating' && <><Zap size={14} className="animate-pulse" style={{ color: theme.accent }} /> Handing off to agent queue with full context…</>}
              </div>
            )}
            <div ref={transcriptEndRef} />
            {/* Item 42: Agent Handoff Panel overlay */}
            {callState === 'escalating' && <AgentHandoffPanel theme={theme} darkMode={darkMode} />}
          </div>

          <div className="absolute bottom-0 left-0 right-0">
            <div className="h-10 w-full" style={{ background: `linear-gradient(to top, ${theme.transcriptBg}, transparent)` }}></div>
            <div className="border-t shadow-[0_-10px_40px_rgba(0,0,0,0.05)]" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
              {/* Item 41: larger centered visualizer */}
              <div className="h-24 md:h-28 flex items-center justify-center border-b gap-8" style={{ borderColor: theme.surface }}>
                <CircularVisualizer state={callState} theme={theme} analyser={analyserRef} />
              </div>

              {/* Simulation deck */}
              <div className="p-3 md:p-4" style={{ backgroundColor: theme.surface }}>
                <div className="flex items-center gap-3 overflow-x-auto pb-1">
                  <span className="text-[11px] font-bold uppercase tracking-widest whitespace-nowrap px-3 py-1.5 rounded-md border" style={{ color: theme.inkMuted, backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>Customer Replies</span>
                  {liveCall && micSupported() && (
                    <button
                      onClick={toggleMic}
                      title={micOn ? 'Stop microphone' : 'Speak to the bot (real mic → Whisper STT)'}
                      aria-pressed={micOn}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-full border text-sm font-semibold whitespace-nowrap transition-all shadow-sm focus-visible:ring-2"
                      style={micOn
                        ? { backgroundColor: theme.accent, borderColor: theme.accent, color: '#fff' }
                        : { backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.ink }}
                    >
                      <Mic size={14} className={micOn ? 'animate-pulse' : ''} />
                      {micOn ? 'Listening…' : 'Mic'}
                    </button>
                  )}
                  {callState === 'listening' ? (
                    scenario.chips.map((chip, idx) => {
                      const isDemoTarget = demoStepTooltip?.id === `chip-${idx}`;
                      return (
                        <div key={chip.label} className="relative">
                          <button onClick={() => handleChip(chip)} className={`px-4 py-2 rounded-full border text-sm font-medium transition-all shadow-sm whitespace-nowrap focus-visible:ring-2 ${isDemoTarget ? 'ring-2 ring-offset-2 ring-blue-500 scale-105 z-10' : ''}`} style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.ink }}>
                            {chip.label}
                          </button>
                          {isDemoTarget && (
                            <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 bg-blue-600 text-white text-[10px] font-bold px-2 py-1 rounded whitespace-nowrap animate-in slide-in-from-bottom-2 z-20 shadow-lg">
                              {demoStepTooltip.text}
                              <div className="absolute top-full left-1/2 -translate-x-1/2 border-[3px] border-transparent border-t-blue-600"></div>
                            </div>
                          )}
                        </div>
                      );
                    })
                  ) : (
                    <span className="text-sm italic px-2 whitespace-nowrap" style={{ color: theme.inkMuted }}>
                      {callState === 'bot_speaking' ? 'Bot is speaking — try a barge-in or backchannel →' : 'Waiting for interaction window…'}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 overflow-x-auto pt-2">
                  <span className="text-[11px] font-bold uppercase tracking-widest whitespace-nowrap px-3 py-1.5 rounded-md border" style={{ color: theme.accent, backgroundColor: darkMode ? '#7C2D12' + '20' : '#FFF0EB', borderColor: darkMode ? '#9A3412' : '#F5C0AD' }}>Edge Cases</span>
                  <EdgeChip icon={<Square size={12} fill="currentColor" />} label="Barge-In (interrupt)" enabled={callState === 'bot_speaking'} onClick={handleBargeIn} title="Customer talks over the bot — Gate 2 passes, TTS_KILL within 150ms" theme={theme} demoTooltip={demoStepTooltip?.id === 'barge-in' ? demoStepTooltip.text : null} />
                  <EdgeChip icon={<Volume2 size={12} />} label={'Backchannel "Hmm"'} enabled={callState === 'bot_speaking'} onClick={handleBackchannel} title="Listening sound — Gate 2 ignores it, TTS continues" theme={theme} />
                  <EdgeChip icon={<Languages size={12} />} label="Speak Marathi" enabled={callState === 'listening'} onClick={handleUnsupported} title="Unsupported language → graceful fallback + agent escalation" theme={theme} />
                  {/* Item 2: language switch chip */}
                  <EdgeChip icon={<Languages size={12} />} label="Switch to English" enabled={callState === 'listening'} onClick={handleLanguageSwitch} title="Mid-call language switch — STT detects change, TTS adjusts" theme={theme} demoTooltip={demoStepTooltip?.id === 'lang-switch' ? demoStepTooltip.text : null} />
                  {/* Item 10: background noise chip */}
                  <EdgeChip icon={<Mic size={12} />} label="Background Noise" enabled={callState === 'listening'} onClick={handleBackgroundNoise} title="Non-speech audio — VAD filters it, no-input timer NOT reset" theme={theme} demoTooltip={demoStepTooltip?.id === 'noise' ? demoStepTooltip.text : null} />
                  {/* Item 45: disable Drop during connecting */}
                  <EdgeChip icon={<WifiOff size={12} />} label="Drop Network" enabled={isLive && callState !== 'reconnecting' && callState !== 'connecting'} onClick={handleNetworkDrop} title="Socket drop → 15s Redis grace TTL → resume with same Session ID" theme={theme} />
                  {/* Item 11: no-reconnect chip */}
                  <EdgeChip icon={<Timer size={12} />} label="Drop (no reconnect)" enabled={callState === 'reconnecting'} onClick={simulateNoReconnect} title="Block reconnect — let 15s grace TTL expire → call_dropped" theme={theme} />
                  <EdgeChip icon={<Timer size={12} />} label="Stay Silent" enabled={callState === 'listening'} onClick={() => { }} title="Just wait — 5s silence triggers a re-prompt, 5 more closes the call" passive theme={theme} />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}


// ============================================================================
// Simulation Chips
// ============================================================================
function EdgeChip({ icon, label, enabled, onClick, title, theme, passive, demoTooltip }: { icon: React.ReactNode; label: string; enabled: boolean; onClick: () => void; title: string; theme: Theme; passive?: boolean; demoTooltip?: string | null }) {
  const isDemoTarget = !!demoTooltip;
  return (
    <div className="relative">
      <button title={title} onClick={onClick} disabled={!enabled} className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-xs font-medium transition-all whitespace-nowrap focus-visible:ring-2 ${passive ? 'shadow-none' : 'shadow-sm'} ${!enabled ? 'opacity-40 cursor-not-allowed' : 'hover:-translate-y-px'} ${isDemoTarget ? 'ring-2 ring-offset-2 ring-blue-500 scale-105 z-10' : ''}`} style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: passive ? theme.inkMuted : theme.ink }}>
        <span style={{ color: enabled ? (passive ? theme.inkMuted : theme.ink) : theme.inkMuted }}>{icon}</span> {label}
      </button>
      {isDemoTarget && (
        <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 bg-blue-600 text-white text-[10px] font-bold px-2 py-1 rounded whitespace-nowrap animate-in slide-in-from-bottom-2 z-20 shadow-lg">
          {demoTooltip}
          <div className="absolute top-full left-1/2 -translate-x-1/2 border-[3px] border-transparent border-t-blue-600"></div>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Transcript bubbles
// ============================================================================
function GuardrailItem({ active, label, pulsing, theme }: { active: boolean; label: string; pulsing?: boolean; theme: Theme }) {
  return (
    <div className="flex items-center gap-2 text-sm p-2.5 rounded-lg border shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      {active ? <CheckCircle2 size={16} className={`text-green-600 dark:text-green-400 shrink-0 ${pulsing ? 'animate-pulse' : ''}`} /> : <AlertCircle size={16} className="shrink-0" style={{ color: theme.rule }} />}
      <span className={`text-xs font-medium`} style={{ color: active ? theme.ink : theme.inkMuted }}>{label}</span>
    </div>
  );
}

// ============================================================================
// Items 9, 28, 41: Amplitude-reactive circular waveform visualiser
// ============================================================================
function CircularVisualizer({ state, theme, analyser }: { state: CallState; theme: Theme; analyser?: React.RefObject<AnalyserNode | null> }) {
  const config: Record<string, { color: string; text: string; active: boolean }> = {
    idle: { color: '#CBD5E1', text: 'Connection Idle', active: false },
    connecting: { color: '#94A3B8', text: 'Handshaking…', active: true },
    bot_speaking: { color: theme.primary, text: 'KOEL Speaking', active: true },
    user_speaking: { color: '#16A34A', text: 'Receiving Audio', active: true },
    processing: { color: '#EAB308', text: 'Orchestrator Logic', active: true },
    listening: { color: '#7DA2FF', text: 'Listening for Input…', active: true },
    reconnecting: { color: '#F97316', text: 'Session Recovery…', active: true },
    escalating: { color: theme.accent, text: 'Escalating to Agent', active: true },
    ended: { color: '#DC2626', text: 'Session Closed', active: false },
  };
  const { color, text, active } = config[state] || config.idle;
  const bars = 24;
  const svgRef = useRef<SVGSVGElement>(null);
  const amplitudesRef = useRef<number[]>(new Array(bars).fill(0));
  const rafRef = useRef<number>(0);
  const reducedMotion = useRef(typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  useEffect(() => {
    if (reducedMotion.current) return;

    // Per-state amplitude gain — applied to real analyser data when available,
    // otherwise used to drive a graceful fallback animation.
    const stateGain = (): number => {
      if (state === 'bot_speaking' || state === 'user_speaking') return 1;
      if (state === 'escalating') return 0.7;
      if (state === 'processing' || state === 'connecting' || state === 'reconnecting') return 0.5;
      if (state === 'listening') return 0.3;
      return 0.05;
    };

    const freq = new Uint8Array(bars);

    const animate = () => {
      const svg = svgRef.current;
      if (!svg) { rafRef.current = requestAnimationFrame(animate); return; }

      const lines = svg.querySelectorAll('line');
      const amps = amplitudesRef.current;

      // Read real spectrum data off the AnalyserNode when the audio graph is
      // live; fall back to a per-state synthetic shape otherwise.
      const node = analyser?.current ?? null;
      let live = false;
      if (node) {
        try { node.getByteFrequencyData(freq); live = true; } catch { live = false; }
      }
      const gain = stateGain();

      for (let i = 0; i < bars; i++) {
        let target = 0;
        if (live) {
          // 0..255 → bar length, scaled by the call-state gain.
          target = (freq[i] / 255) * 16 * gain + 0.5;
        } else if (state === 'bot_speaking' || state === 'user_speaking') {
          target = 4 + Math.random() * 10;
        } else if (state === 'listening') {
          target = 1 + Math.random() * 3;
        } else if (state === 'processing' || state === 'connecting' || state === 'reconnecting') {
          target = 2 + Math.random() * 5;
        } else if (state === 'escalating') {
          target = 3 + Math.random() * 7;
        } else {
          target = 0.5;
        }
        amps[i] = amps[i] + (target - amps[i]) * 0.15;

        const a = (i / bars) * Math.PI * 2;
        const r1 = 34;
        const r2 = r1 + amps[i];
        const line = lines[i];
        if (line) {
          line.setAttribute('x1', String(40 + r1 * Math.cos(a)));
          line.setAttribute('y1', String(40 + r1 * Math.sin(a)));
          line.setAttribute('x2', String(40 + r2 * Math.cos(a)));
          line.setAttribute('y2', String(40 + r2 * Math.sin(a)));
        }
      }

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(rafRef.current);
  }, [state, bars, analyser]);

  return (
    <div className="flex items-center gap-5">
      {/* Item 41: larger visualizer — 80x80 */}
      <div className="relative w-20 h-20 flex items-center justify-center">
        {active && !reducedMotion.current && (
          <>
            <div className="absolute inset-0 rounded-full animate-ping opacity-15" style={{ backgroundColor: color, animationDuration: '1.6s' }}></div>
            <div className="absolute inset-2 rounded-full animate-pulse opacity-20" style={{ backgroundColor: color }}></div>
          </>
        )}
        <div className="absolute inset-4 rounded-full transition-colors duration-300 flex items-center justify-center" style={{ backgroundColor: color }}>
          <Radio size={20} className="text-white" />
        </div>
        <svg ref={svgRef} viewBox="0 0 80 80" className="absolute inset-0 w-full h-full">
          {Array.from({ length: bars }).map((_, i) => {
            const a = (i / bars) * Math.PI * 2;
            const r1 = 34;
            const r2 = active ? 37 : 35;
            return (
              <line
                key={i}
                x1={40 + r1 * Math.cos(a)} y1={40 + r1 * Math.sin(a)}
                x2={40 + r2 * Math.cos(a)} y2={40 + r2 * Math.sin(a)}
                stroke={color} strokeWidth="2" strokeLinecap="round"
              />
            );
          })}
        </svg>
      </div>
      <div>
        <span className="block text-xs font-bold uppercase tracking-widest" style={{ color: theme.ink }}>{text}</span>
        <span className="block text-[10px] font-medium mt-0.5" style={{ color: theme.inkMuted }}>Web Audio API · amplitude-reactive waveform</span>
      </div>
    </div>
  );
}
