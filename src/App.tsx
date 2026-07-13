import React, { useState, useEffect, useRef, useCallback, useMemo, lazy, Suspense } from 'react';
import {
  getBots as apiGetBots,
  createBot,
  patchBot,
  getAllStats,
  refreshToken,
  saveToken,
  loadStoredToken,
  clearStoredToken,
  tokenExpiresInMs,
  USE_CASE_CUSTOMER,
  type BackendBot,
  type NewBotPayload,
  type BotStats,
} from './lib/koel';
import {
  Settings, Clock, PhoneCall,
  BarChart2, CheckCircle2, X,
  LogOut, Sun, Moon, Menu, Edit3, BookOpen
} from 'lucide-react';
import type {
  ViewState, UseCase, Bot, BotConfig, CallHistory,
} from './types';
import { getTheme } from './lib/theme';
import { formatRelativeTime } from './lib/format';
// Pure helper (no React) — safe to import on the eager path without pulling the
// flow-editor canvas components into the initial bundle.
import { coerceFlow } from './components/FlowEditor/flowModel';
import { LoginView } from './views/LoginView';
import { DashboardView } from './views/DashboardView';

// Login + Dashboard are the entry/landing screens — kept eager so first paint
// has no loading flash. The rest are code-split (React.lazy): dashboard-only
// users never download the call view / builder / KB bundles (roadmap Q-1).
const HistoryView = lazy(() => import('./views/HistoryView').then(m => ({ default: m.HistoryView })));
const ConfigView = lazy(() => import('./views/ConfigView').then(m => ({ default: m.ConfigView })));
const BotBuilderView = lazy(() => import('./views/BotBuilderView').then(m => ({ default: m.BotBuilderView })));
const KnowledgeBaseView = lazy(() => import('./views/KnowledgeBaseView').then(m => ({ default: m.KnowledgeBaseView })));
const ActiveCallView = lazy(() => import('./views/ActiveCallView').then(m => ({ default: m.ActiveCallView })));

// Shared domain types now live in ./types; getTheme in ./lib/theme (Q-1 decomposition).


// ============================================================================
// Mock data — bots, CRM profiles, scenarios, seeded history
// ============================================================================
const now = Date.now();

// Named instances mirror the PRD §09 Screen 1 examples (HDFC Credit Card,
// ICICI Credit Card, Insurance Bot) while keeping all four use cases covered.
const INITIAL_BOTS: Bot[] = [
  { id: 'b1', name: 'HDFC Credit Card', useCase: 'Credit Card', language: 'Hinglish', status: 'Active', calls: 1240, conversionNum: 14.2, conversion: '14.2%', avgDuration: '1m 38s', escalationRate: '9.1%', lastActive: '2 min ago', lastActiveTs: now - 120000 },
  { id: 'b2', name: 'ICICI Credit Card', useCase: 'Credit Card', language: 'Hinglish', status: 'Active', calls: 2110, conversionNum: 12.7, conversion: '12.7%', avgDuration: '1m 51s', escalationRate: '8.4%', lastActive: '40 sec ago', lastActiveTs: now - 40000 },
  { id: 'b3', name: 'Insurance Bot', useCase: 'Insurance', language: 'English', status: 'Active', calls: 450, conversionNum: 8.4, conversion: '8.4%', avgDuration: '3m 12s', escalationRate: '18.6%', lastActive: '6 min ago', lastActiveTs: now - 360000 },
  { id: 'b4', name: 'ICICI Pre-Due Collections', useCase: 'Collections', language: 'Hindi', status: 'Active', calls: 8900, conversionNum: 32.1, conversion: '32.1%', avgDuration: '0m 54s', escalationRate: '4.3%', lastActive: '14 sec ago', lastActiveTs: now - 14000 },
  { id: 'b5', name: 'Axis Loan Origination', useCase: 'Personal Loan', language: 'Hinglish', status: 'Active', calls: 3200, conversionNum: 21.5, conversion: '21.5%', avgDuration: '2m 05s', escalationRate: '11.8%', lastActive: '1 hr ago', lastActiveTs: now - 3600000 },
];

// Item 33: offer amount added to Credit Card CRM
const MOCK_HISTORY_CALLS: CallHistory[] = [
  {
    id: 'CALL-9921', bot: 'HDFC Card Cross-Sell', language: 'Hinglish', durationStr: '1m 45s', durationSec: 105, outcome: 'Escalated to Agent', dateStr: '09 Jun, 14:30', timestamp: 1781015400000, status: 'orange',
    customer: 'Rahul Sharma', customerId: 'CUST-882910', phone: '+91 98*** ***21', avgLatencyMs: 268,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', latency: { stt: 0, nlp: 0, tts: 180, total: 180 }, text: 'Namaste Rahul ji, main HDFC Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved Regalia credit card offer hai. Do minute baat kar sakte hain?' },
      { id: 2, kind: 'user', text: 'Haan boliye, but mere paas already do cards hain.' },
      { id: 3, kind: 'meta', text: 'RAG Retrieval: Objection Handling — Multiple Cards' },
      { id: 4, kind: 'bot', tone: 'empathetic', latency: { stt: 70, nlp: 130, tts: 85, total: 285 }, text: 'Bilkul samajh sakti hoon. Yeh card lounge access aur 4x reward points deta hai dining par — aapke existing cards se alag benefit. Pehle saal ki fee bhi waived hai. Interested hain?' },
      { id: 5, kind: 'user', text: 'Theek hai, thoda detail chahiye. Koi bande se baat kara do.' },
      { id: 6, kind: 'meta', text: 'Escalation triggered: HIGH_INTEREST · Full transcript + CRM context handed to agent queue.' }
    ]
  },
  {
    id: 'CALL-9920', bot: 'ICICI Pre-Due Collections', language: 'Hindi', durationStr: '0m 32s', durationSec: 32, outcome: 'Promise to Pay', dateStr: '09 Jun, 12:15', timestamp: 1781007300000, status: 'green',
    customer: 'Amit Patel', customerId: 'CUST-554102', phone: '+91 99*** ***44', avgLatencyMs: 241,
    transcript: [
      { id: 1, kind: 'bot', tone: 'firm', text: 'Namaste Amit ji, main ICICI Bank ki taraf se KOEL bol rahi hoon — yeh ek automated reminder call hai. Aapke credit card ka ₹12,500 ka bill 15 June ko due hai. Kya aap payment plan confirm kar sakte hain?' },
      { id: 2, kind: 'user', text: 'Yes, I know. I will pay it tomorrow.' },
      { id: 3, kind: 'bot', tone: 'empathetic', text: 'Thank you for confirming. I have recorded your promise to pay for tomorrow. I will also send a payment link via SMS for your convenience. Have a great day!' },
      { id: 4, kind: 'meta', text: 'Action: PROMISE_TO_PAY recorded · SMS_PAYMENT_LINK_SENT' }
    ]
  },
  {
    id: 'CALL-9919', bot: 'HDFC Card Cross-Sell', language: 'Hinglish', durationStr: '0m 12s', durationSec: 12, outcome: 'Customer Hung Up', dateStr: '09 Jun, 09:10', timestamp: 1780996200000, status: 'red',
    customer: 'Priya Singh', customerId: 'CUST-882910', phone: '+91 87*** ***19', avgLatencyMs: 255,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', text: 'Namaste Priya ji, main HDFC Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved...', interrupted: true },
      { id: 2, kind: 'user', text: 'Mujhe koi card nahi chahiye, please mat call karo.' },
      { id: 3, kind: 'meta', text: 'Opt-out intent detected · CRM flagged DO_NOT_CALL · Session disconnected by user.' }
    ]
  },
  {
    id: 'CALL-9918', bot: 'MaxLife Term Pitch', language: 'English', durationStr: '4m 10s', durationSec: 250, outcome: 'Completed — Interested', dateStr: '08 Jun, 16:45', timestamp: 1780937100000, status: 'green',
    customer: 'Vikram Desai', customerId: 'CUST-119283', phone: '+91 76*** ***90', avgLatencyMs: 289,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', text: 'Hello Vikram sir, this is KOEL calling from MaxLife — this is an automated call. You recently checked term insurance premiums on our app...' },
      { id: 2, kind: 'user', text: 'Yes, but the premium was a bit high.' },
      { id: 3, kind: 'meta', text: 'RAG Retrieval: Premium Objection Handling' },
      { id: 4, kind: 'filler', text: '"Hmm, let me pull that up…"' },
      { id: 5, kind: 'bot', tone: 'empathetic', latency: { stt: 80, nlp: 1240, tts: 90, total: 1410 }, text: 'I understand. However, if you opt for annual payments instead of monthly, you get a 5% discount. Plus, at your age, locking in this premium now saves you money in the long run. Would you like me to send the revised quote?' },
      { id: 6, kind: 'user', text: 'Okay, send it on WhatsApp.' },
      { id: 7, kind: 'meta', text: 'Action: WHATSAPP_QUOTE_SENT · VERBAL_CONSENT captured' }
    ]
  },
  {
    id: 'CALL-9917', bot: 'ICICI Pre-Due Collections', language: 'Hindi', durationStr: '1m 05s', durationSec: 65, outcome: 'Wrong Number', dateStr: '08 Jun, 10:20', timestamp: 1780914000000, status: 'gray',
    customer: 'Unknown User', customerId: 'N/A', phone: '+91 90*** ***22', avgLatencyMs: 232,
    transcript: [
      { id: 1, kind: 'bot', tone: 'neutral', text: 'Hello, am I speaking with Mr. Suresh? Main ICICI Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai.' },
      { id: 2, kind: 'user', text: 'Nahi bhai, wrong number hai.' },
      { id: 3, kind: 'bot', tone: 'empathetic', text: 'Oh, I apologize for the inconvenience. We will update our records. Have a good day.' },
      { id: 4, kind: 'meta', text: 'CRM Flag: UPDATE_WRONG_NUMBER' }
    ]
  },
  {
    id: 'CALL-9916', bot: 'Axis Loan Origination', language: 'Hinglish', durationStr: '0m 48s', durationSec: 48, outcome: 'Call Dropped', dateStr: '07 Jun, 18:05', timestamp: 1780855500000, status: 'gray',
    customer: 'Sneha Iyer', customerId: 'CUST-773645', phone: '+91 88*** ***07', avgLatencyMs: 274,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', text: 'Namaste Sneha ji, main Axis Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke liye ₹4 lakh tak ka pre-approved personal loan offer hai...' },
      { id: 2, kind: 'meta', text: 'WebSocket disconnected · Redis session held on 15s grace TTL' },
      { id: 3, kind: 'meta', text: 'No reconnect within grace window · Outcome: call_dropped · CRM flagged for callback' }
    ]
  },
];

// ============================================================================
// Helpers
// ============================================================================
const USE_CASE_LABEL: Record<string, UseCase> = {
  credit_card: 'Credit Card',
  collections: 'Collections',
  insurance: 'Insurance',
  personal_loan: 'Personal Loan',
};

// Map the frontend config form to the backend's bot-create payload (enum-safe).
function configToNewBot(config: BotConfig): NewBotPayload {
  const uc = `${config.useCase} ${config.name}`.toLowerCase();
  const use_case: NewBotPayload['use_case'] =
    /collection/.test(uc) ? 'collections'
    : /insur|term|life/.test(uc) ? 'insurance'
    : /loan/.test(uc) ? 'personal_loan'
    : /card|credit/.test(uc) ? 'credit_card'
    : 'custom';
  const language = config.language === 'Hindi' ? 'hi' : config.language === 'English' ? 'en' : 'hinglish';
  return {
    name: config.name || 'New Bot',
    use_case,
    language,
    voice_gender: 'female',
    speaking_rate: config.speed,
    greeting_template: config.greeting,
    rag_enabled: config.ragEnabled,
    rag_index: config.ragIndex || null,
    default_tone: config.tone,
    ambient_profile: config.ambientProfile,
    // Round-trips to the backend's bot config JSON. `stt_mode` and `flow` (6.6)
    // share the one blob; PATCH replaces `config` wholesale so both must ship
    // together. The scripted flow is consumed by the backend interpreter from
    // exactly `config.flow` (koel/voicebot/pipeline/flow.py).
    config: { stt_mode: config.sttMode, ...(config.flow ? { flow: config.flow } : {}) },
  };
}

function backendBotToBot(b: BackendBot): Bot {
  const useCase = USE_CASE_LABEL[b.use_case] || 'Credit Card';
  const langLabel = b.language === 'hi' ? 'Hindi' : b.language === 'en' ? 'English' : 'Hinglish';
  // Hydrate the persisted STT mode so the config form seeds the operator's choice.
  const sttMode: Bot['sttMode'] = b.config?.stt_mode === 'ws' ? 'ws' : 'chunk';
  // Hydrate the persisted scripted flow (6.6) — coerced defensively; a malformed
  // or absent flow yields undefined and the editor seeds a default starter.
  const flow = coerceFlow(b.config?.flow) ?? undefined;
  return {
    id: b.id,
    name: b.name,
    useCase,
    language: langLabel,
    status: b.active ? 'Active' : 'Paused',
    calls: 0,
    conversionNum: 0,
    conversion: '—',
    avgDuration: '—',
    escalationRate: '—',
    lastActive: 'live',
    lastActiveTs: Date.now(),
    backendId: b.id,
    backendCustomerId: USE_CASE_CUSTOMER[b.use_case],
    sttMode,
    flow,
  };
}

// Fold real call-log aggregates (AE /v1/stats) into a bot's displayed metrics.
function mergeStats(bot: Bot, stats: BotStats | undefined): Bot {
  if (!stats || stats.total_calls === 0) return bot;
  const conv = stats.conversion_rate * 100;
  const totalSec = Math.round(stats.avg_duration_ms / 1000);
  return {
    ...bot,
    calls: stats.total_calls,
    conversionNum: conv,
    conversion: `${conv.toFixed(1)}%`,
    avgDuration: `${Math.floor(totalSec / 60)}m ${String(totalSec % 60).padStart(2, '0')}s`,
    escalationRate: `${(stats.escalation_rate * 100).toFixed(1)}%`,
  };
}

export default function KoelApp() {
  const [authed, setAuthed] = useState(false);
  // Live backend integration: token + flag. When live, bots/calls hit VB_Svc.
  const [token, setToken] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  // True while we attempt to restore a persisted session on load (Q-2) — avoids
  // flashing the login screen before the silent refresh resolves.
  const [restoring, setRestoring] = useState<boolean>(() => !!loadStoredToken());
  const [currentView, setCurrentView] = useState<ViewState>('dashboard');
  const [activeBotId, setActiveBotId] = useState<string | null>(null);
  const [configBotId, setConfigBotId] = useState<string | null>(null); // Item 1/30
  const [bots, setBots] = useState<Bot[]>(INITIAL_BOTS); // Item 6 — lifted
  const [botConfigs, setBotConfigs] = useState<Record<string, BotConfig>>({}); // Item 5
  const [completedCalls, setCompletedCalls] = useState<CallHistory[]>(MOCK_HISTORY_CALLS);
  const [toast, setToast] = useState<string | null>(null);
  const [darkMode, setDarkMode] = useState(false); // Item 61
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false); // Item 53
  const toastTimerRef = useRef<number | null>(null); // Item 48

  const theme = useMemo(() => getTheme(darkMode), [darkMode]);

  // Item 48: fix overlapping toasts
  const showToast = useCallback((msg: string) => {
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    setToast(msg);
    toastTimerRef.current = window.setTimeout(() => setToast(null), 3000);
  }, []);

  // Refresh the relative "last active" label in real time. Call counts come from
  // real backend aggregates (AE), not a random nudge, so they're left untouched.
  useEffect(() => {
    const iv = window.setInterval(() => {
      setBots(prev => prev.map(b => ({
        ...b,
        lastActive: formatRelativeTime(b.lastActiveTs),
      })));
    }, 15000);
    return () => window.clearInterval(iv);
  }, []);

  const navigateToCall = useCallback((botId: string) => {
    setActiveBotId(botId);
    setCurrentView('call');
    setMobileMenuOpen(false);
  }, []);

  // Item 1/30: config bound to bot
  const navigateToConfig = useCallback((botId: string) => {
    setConfigBotId(botId);
    setCurrentView('config');
    setMobileMenuOpen(false);
  }, []);

  const onCallCompleted = useCallback((callData: CallHistory) => {
    setCompletedCalls(prev => [callData, ...prev]);
    // Item 6: update the bot stats after a call
    setBots(prev => prev.map(b =>
      b.name === callData.bot
        ? { ...b, calls: b.calls + 1, lastActive: 'just now', lastActiveTs: Date.now() }
        : b
    ));
  }, []);

  // Item 5: save config
  const handleConfigSave = useCallback(async (botId: string, config: BotConfig) => {
    setBotConfigs(prev => ({ ...prev, [botId]: config }));
    if (botId === 'new') {
      // Live: persist the new bot to VB_Svc so the call pipeline can actually
      // use it (PRD §06 — new use cases are configuration, not code).
      if (live && token) {
        try {
          const created = await createBot(token, configToNewBot(config));
          setBots(prev => [...prev, backendBotToBot(created)]);
          setBotConfigs(prev => ({ ...prev, [created.id]: config }));
          showToast('Bot created on VB_Svc — available for live calls.');
          setCurrentView('dashboard');
          return;
        } catch {
          showToast('Could not save to backend — added locally only.');
        }
      }
      const newId = `b${Date.now().toString(36)}`;
      setBots(prev => [...prev, {
        id: newId, name: config.name, useCase: config.useCase as UseCase, language: config.language,
        status: 'Active', calls: 0, conversionNum: 0, conversion: '0%', avgDuration: '0m 00s',
        escalationRate: '0%', lastActive: 'just now', lastActiveTs: Date.now(),
      }]);
      setBotConfigs(prev => ({ ...prev, [newId]: config }));
    } else if (live && token) {
      // Existing bot → persist the edit to VB_Svc (PATCH) so it survives reload
      // and is audited server-side (S/AF). Falls back to local-only on failure.
      const backendId = bots.find(b => b.id === botId)?.backendId;
      if (backendId) {
        try {
          const { use_case: _uc, ...patch } = configToNewBot(config);
          await patchBot(token, backendId, patch);
          showToast('Configuration saved to VB_Svc — effective on next session start.');
          setCurrentView('dashboard');
          return;
        } catch {
          showToast('Could not save to backend — kept locally only.');
        }
      }
    }
    showToast('Configuration saved — takes effect on next session start.');
    setCurrentView('dashboard');
  }, [showToast, live, token, bots]);

  // Fetch live bots + analytics after auth (fresh login or a restored session).
  // Returns true when real bots loaded; best-effort — the caller owns the toast.
  const hydrateLiveBots = useCallback(async (tok: string): Promise<boolean> => {
    try {
      const backendBots = await apiGetBots(tok);
      if (backendBots.length) {
        // Merge real call-log aggregates (AE) into the loaded bots.
        let stats: BotStats[] = [];
        try {
          stats = await getAllStats(tok);
        } catch {
          /* stats are best-effort; bots still render with zeroed metrics */
        }
        const byId = new Map(stats.map(s => [s.bot_id, s]));
        setBots(backendBots.map(b => mergeStats(backendBotToBot(b), byId.get(b.id))));
        return true;
      }
    } catch {
      /* fall through — caller shows the degraded-mode toast */
    }
    return false;
  }, []);

  const handleLogin = useCallback(async (opts: { live: boolean; token: string | null }) => {
    setLive(opts.live);
    setToken(opts.token);
    setAuthed(true);
    setCurrentView('dashboard');
    if (opts.live && opts.token) {
      saveToken(opts.token); // persist so a reload doesn't log the operator out (Q-2)
      const ok = await hydrateLiveBots(opts.token);
      showToast(ok
        ? 'Connected to live VB_Svc — bots + analytics loaded from the backend.'
        : 'Logged in, but failed to load live bots — showing demo bots.');
    } else {
      showToast('Backend offline — running in demo simulation mode.');
    }
  }, [showToast, hydrateLiveBots]);

  const handleLogout = useCallback(() => {
    clearStoredToken();
    setToken(null);
    setLive(false);
    setAuthed(false);
  }, []);

  // Restore a persisted session on load (Q-2): a still-valid stored token is
  // swapped for a fresh one via /v1/auth/refresh; an expired/invalid one is
  // cleared so the operator simply logs in again. Runs once.
  const restoreAttempted = useRef(false);
  useEffect(() => {
    if (restoreAttempted.current) return;
    restoreAttempted.current = true;
    const stored = loadStoredToken();
    if (!stored) {
      setRestoring(false);
      return;
    }
    (async () => {
      try {
        const fresh = await refreshToken(stored);
        saveToken(fresh);
        setToken(fresh);
        setLive(true);
        setAuthed(true);
        setCurrentView('dashboard');
        await hydrateLiveBots(fresh);
      } catch {
        clearStoredToken(); // expired/invalid → require a fresh login
      } finally {
        setRestoring(false);
      }
    })();
  }, [hydrateLiveBots]);

  // Proactive silent refresh (Q-2): renew the access token at ~80% of its
  // remaining lifetime so a live session never expires mid-shift. Reschedules on
  // every token change; a failed refresh is left for the next 401/reload.
  useEffect(() => {
    if (!live || !token) return;
    const ttl = tokenExpiresInMs(token);
    const delay = ttl && ttl > 0 ? Math.max(ttl * 0.8, 5000) : 600_000;
    const id = window.setTimeout(async () => {
      try {
        const fresh = await refreshToken(token);
        saveToken(fresh);
        setToken(fresh);
      } catch {
        /* refresh failed — a later call's 401 or a reload forces re-login */
      }
    }, delay);
    return () => window.clearTimeout(id);
  }, [live, token]);

  if (restoring) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ backgroundColor: theme.primary, fontFamily: '"DM Sans", sans-serif' }}>
        <div className="text-sm text-white/80">Restoring session…</div>
      </div>
    );
  }
  if (!authed) {
    return <LoginView onLogin={handleLogin} theme={theme} />;
  }

  const viewLabels: Record<ViewState, string> = {
    dashboard: 'Bot Dashboard',
    call: 'Active Call',
    config: 'Configuration',
    history: 'Call History',
    builder: 'Bot Builder',
    knowledge: 'Knowledge Base',
  };

  const operateNav: { view: ViewState; icon: React.ReactNode; label: string }[] = [
    { view: 'dashboard', icon: <BarChart2 size={16} />, label: 'Bot Dashboard' },
    { view: 'call', icon: <PhoneCall size={16} />, label: 'Active Call' },
    { view: 'history', icon: <Clock size={16} />, label: 'Call History' },
  ];

  const manageNav: { view: ViewState; icon: React.ReactNode; label: string }[] = [
    { view: 'builder', icon: <Edit3 size={16} />, label: 'Bot Builder' },
    { view: 'config', icon: <Settings size={16} />, label: 'Configuration' },
    { view: 'knowledge', icon: <BookOpen size={16} />, label: 'Knowledge Base' },
  ];

  return (
    <div className={`h-screen overflow-hidden grid grid-cols-1 md:grid-cols-[216px_1fr] ${darkMode ? 'dark' : ''}`} style={{ fontFamily: '"DM Sans", sans-serif', color: theme.ink, backgroundColor: theme.surface }}>
      {/* ---- LEFT SIDEBAR ---- */}
      <aside className="flex flex-col border-r overflow-hidden max-md:hidden" style={{ backgroundColor: theme.sidebarBg, borderColor: theme.rule }}>
        {/* Brand */}
        <div className="flex items-center gap-2.5 px-5 py-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
          <div className="w-7 h-7 flex items-center justify-center text-white text-[15px] font-semibold" style={{ backgroundColor: theme.primary, borderRadius: 7, fontFamily: '"Lora", serif' }}>K</div>
          <div>
            <span className="font-semibold text-lg tracking-tight block leading-tight" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>KOEL</span>
            <span className="text-[10px] tracking-widest uppercase block -mt-0.5" style={{ color: theme.faint }}>Voice Bot Platform</span>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-2.5 py-3 flex flex-col gap-0.5 overflow-y-auto">
          <span className="text-[10.5px] font-medium tracking-widest uppercase px-2.5 pt-3 pb-1.5" style={{ color: theme.faint }}>Operate</span>
          {operateNav.map(ni => (
            <NavItem key={ni.view} active={currentView === ni.view} onClick={() => setCurrentView(ni.view)} icon={ni.icon} label={ni.label} theme={theme} />
          ))}
          <span className="text-[10.5px] font-medium tracking-widest uppercase px-2.5 pt-5 pb-1.5" style={{ color: theme.faint }}>Manage</span>
          {manageNav.map(ni => (
            <NavItem key={ni.view} active={currentView === ni.view} onClick={() => setCurrentView(ni.view)} icon={ni.icon} label={ni.label} theme={theme} />
          ))}
        </nav>

        {/* Env status */}
        <div className="mx-2.5 mb-2.5 p-2.5 border rounded-lg" style={{ backgroundColor: theme.inset, borderColor: theme.rule }}>
          <div className="flex justify-between items-center text-xs" style={{ color: theme.inkMuted }}>
            <span className="flex items-center gap-1.5">
              <span className="w-[7px] h-[7px] rounded-full inline-block" style={{ backgroundColor: theme.greenMid }}></span>
              VB_Svc
            </span>
            <span style={{ fontFamily: '"DM Mono", monospace', fontSize: 11, color: theme.ink2 }}>ws · connected</span>
          </div>
          <div className="flex justify-between items-center text-xs mt-1.5" style={{ color: theme.faint }}>
            <span style={{ fontSize: 11.5 }}>Env</span>
            <span style={{ fontFamily: '"DM Mono", monospace', fontSize: 11, color: theme.ink2 }}>demo-mvp</span>
          </div>
        </div>
      </aside>

      {/* ---- MAIN COLUMN ---- */}
      <div className="flex flex-col overflow-hidden">
        {/* Topbar */}
        <header className="h-14 flex items-center justify-between px-6 border-b flex-none" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="flex items-center gap-2">
            {/* Mobile hamburger */}
            <button onClick={() => setMobileMenuOpen(!mobileMenuOpen)} className="md:hidden p-2 rounded-lg mr-2" style={{ color: theme.inkMuted }} aria-label="Toggle navigation menu">
              {mobileMenuOpen ? <X size={20} /> : <Menu size={20} />}
            </button>
            {/* Mobile logo */}
            <div className="md:hidden flex items-center gap-2 mr-3 cursor-pointer" onClick={() => setCurrentView('dashboard')}>
              <div className="w-6 h-6 flex items-center justify-center text-white text-xs font-semibold" style={{ backgroundColor: theme.primary, borderRadius: 5, fontFamily: '"Lora", serif' }}>K</div>
              <span className="font-semibold text-base" style={{ fontFamily: '"Lora", serif', color: theme.primary }}>KOEL</span>
            </div>
            {/* Breadcrumb */}
            <span className="hidden md:inline text-[13px]" style={{ color: theme.inkMuted }}>KOEL&nbsp;&nbsp;/&nbsp;&nbsp;<b style={{ color: theme.ink, fontWeight: 500 }}>{viewLabels[currentView]}</b></span>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={() => setDarkMode(!darkMode)} className="w-8 h-8 rounded-full flex items-center justify-center transition-colors" style={{ color: theme.inkMuted }} aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}>
              {darkMode ? <Sun size={15} /> : <Moon size={15} />}
            </button>
            <div className="hidden sm:flex items-center gap-1.5 px-2 py-1 rounded-full text-[11.5px] font-medium" style={{ backgroundColor: theme.greenLight, color: theme.green }}>
              <span className="w-[6px] h-[6px] rounded-full" style={{ backgroundColor: 'currentColor' }}></span>
              All services healthy
            </div>
            <div className="w-[30px] h-[30px] rounded-full flex items-center justify-center text-xs font-medium" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>GA</div>
          </div>
        </header>

        {/* Mobile nav overlay */}
        {mobileMenuOpen && (
          <div className="md:hidden border-b p-4 flex flex-col gap-1 animate-in fade-in" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>
            {[...operateNav, ...manageNav].map(ni => (
              <button key={ni.view} onClick={() => { setCurrentView(ni.view); setMobileMenuOpen(false); }} className="flex items-center gap-2.5 px-3 py-2.5 rounded-md text-sm" style={{ backgroundColor: currentView === ni.view ? theme.primaryLight : 'transparent', color: currentView === ni.view ? theme.primary : theme.ink }}>
                {ni.icon} {ni.label}
              </button>
            ))}
            <hr className="my-2" style={{ borderColor: theme.rule }} />
            <button onClick={() => setDarkMode(!darkMode)} className="flex items-center gap-2.5 px-3 py-2.5 rounded-md text-sm" style={{ color: theme.ink }}>
              {darkMode ? <Sun size={16} /> : <Moon size={16} />} {darkMode ? 'Light Mode' : 'Dark Mode'}
            </button>
            <button onClick={handleLogout} className="flex items-center gap-2.5 px-3 py-2.5 rounded-md text-sm text-red-600">
              <LogOut size={16} /> Log Out
            </button>
          </div>
        )}

        {/* Screen content — lazy views resolve inside the Suspense boundary */}
        <main className="flex-1 overflow-y-auto">
          <Suspense fallback={
            <div className="flex flex-col items-center justify-center h-[60vh] gap-3" role="status" aria-live="polite">
              <div className="w-8 h-8 rounded-full border-2 border-t-transparent animate-spin" style={{ borderColor: theme.primary, borderTopColor: 'transparent' }} />
              <span className="text-[13px] font-medium" style={{ color: theme.inkMuted }}>Loading…</span>
            </div>
          }>
          {currentView === 'dashboard' && <DashboardView bots={bots} onLaunch={navigateToCall} onConfig={navigateToConfig} onBuilder={() => setCurrentView('builder')} theme={theme} />}
          {currentView === 'call' && (
            activeBotId ? (
              <ActiveCallView
                bot={bots.find(b => b.id === activeBotId) || bots[0]}
                onBack={() => setCurrentView('dashboard')}
                onCallCompleted={onCallCompleted}
                theme={theme}
                darkMode={darkMode}
                live={live}
                token={token}
              />
            ) : (
              <div className="flex flex-col items-center justify-center h-[60vh] text-center p-8">
                <PhoneCall size={64} className="mb-6 opacity-20" style={{ color: theme.primary }} />
                <p className="text-xl font-semibold mb-2" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>No Bot Selected</p>
                <p className="text-[13px] max-w-sm" style={{ color: theme.inkMuted }}>Select a bot from the Dashboard and click "Launch Sandbox" to open the active call view.</p>
                <button onClick={() => setCurrentView('dashboard')} className="mt-6 px-5 py-2.5 rounded-md text-white font-medium text-sm" style={{ backgroundColor: theme.primary }}>Go to Dashboard</button>
              </div>
            )
          )}
          {currentView === 'config' && (
            <ConfigView
              bot={configBotId && configBotId !== 'new' ? bots.find(b => b.id === configBotId) : undefined}
              existingConfig={configBotId ? botConfigs[configBotId] : undefined}
              isNew={configBotId === 'new'}
              onSave={(config) => handleConfigSave(configBotId || 'new', config)}
              onCancel={() => setCurrentView('dashboard')}
              theme={theme}
            />
          )}
          {currentView === 'history' && <HistoryView calls={completedCalls} onExported={() => showToast('CSV exported for QA workflows.')} theme={theme} darkMode={darkMode} />}
          {currentView === 'builder' && <BotBuilderView onDeploy={(config) => { handleConfigSave('new', config); showToast('Bot deployed — available on the dashboard.'); }} onCancel={() => setCurrentView('dashboard')} theme={theme} />}
          {currentView === 'knowledge' && <KnowledgeBaseView theme={theme} token={token} onToast={showToast} />}
          </Suspense>
        </main>
      </div>

      {toast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[100] bg-gray-900 text-white text-sm font-medium px-5 py-3 rounded-lg shadow-xl flex items-center gap-2 animate-in fade-in slide-in-from-bottom-2" role="status" aria-live="polite">
          <CheckCircle2 size={16} className="text-green-400" /> {toast}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Login — PRD Section 8: dashboard behind login, seeded demo credentials, JWT
// Items 21 (Turnstile), 60 (credential validation)
// ============================================================================
// ============================================================================
// Shared nav item
// ============================================================================
interface NavItemProps { active: boolean; onClick: () => void; icon: React.ReactNode; label: string; theme: ReturnType<typeof getTheme> }

function NavItem({ active, onClick, icon, label, theme }: NavItemProps) {
  return (
    <button onClick={onClick} className={`flex items-center gap-2.5 px-2.5 py-2 rounded-md text-[13.5px] w-full text-left transition-colors ${active ? 'font-medium' : ''}`} style={{ backgroundColor: active ? theme.primaryLight : 'transparent', color: active ? theme.primary : theme.ink2 }}>
      {icon}
      {label}
    </button>
  );
}

// ============================================================================
// Agent Handoff Panel (Item 42)
// ============================================================================

