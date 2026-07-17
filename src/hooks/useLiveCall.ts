import { useState, useEffect, useRef } from 'react';

import { startSession as apiStartSession, LiveCall, KoelApiError, type LiveCallHandlers } from '../lib/koel';
import { MicCapture } from '../lib/audio';
import { formatTime } from '../lib/format';
import type {
  Bot, CallState, CallHistory, TranscriptMessageData, SessionEvent, ChipDef, Tone,
} from '../types';
import {
  SCENARIOS, MOCK_CUSTOMERS, FILLER_POOLS, LANG_CODES, randLatency, nextId,
} from '../data/callScenarios';

interface UseLiveCallParams {
  bot: Bot;
  onCallCompleted: (call: CallHistory) => void;
  live: boolean;
  token: string | null;
}

// The Active Call sandbox state machine, extracted from ActiveCallView (roadmap
// Q-1) so the view is presentational. Owns demo orchestration, the live WS
// (LiveCall) lifecycle, Web Audio playback graph, mic capture, and all the
// scripted edge-case simulations. Returns the state + actions the view renders.
export function useLiveCall({ bot, onCallCompleted, live, token }: UseLiveCallParams) {
  const scenario = SCENARIOS[bot.useCase];
  const customer = MOCK_CUSTOMERS[bot.useCase];
  // True only when this bot is backed by VB_Svc AND we hold a token.
  const liveCall = live && !!token && !!bot.backendId;

  const [callState, setCallState] = useState<CallState>('idle');
  const [transcript, setTranscript] = useState<TranscriptMessageData[]>([]);
  // Wave 2: current LIVE interim customer transcript (best-effort). Updates in
  // place as more audio arrives; cleared when the authoritative final transcript
  // for the turn lands (see onTranscript) or when a call starts/ends. Empty = none.
  const [partialTranscript, setPartialTranscript] = useState<string>('');
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
  // Mirror of `events` — endCall runs from stale closures (scheduled timers,
  // onEnded/onTtsKill captured at startLiveCall), so it must read the ref, not
  // the closed-over `events` state, or the persisted CallHistory.events is empty.
  const eventsRef = useRef<SessionEvent[]>([]);
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
    // Streaming STT (config.stt_mode='ws'): additionally forward captured PCM
    // frames continuously as non-final chunks. Chunk mode keeps the single
    // per-utterance send below untouched.
    const streaming = bot.sttMode === 'ws';
    try {
      const cap = new MicCapture({
        onUtterance: (b64, fmt, pcm16) => {
          logEvent(`Mic utterance captured (${fmt}) → streaming to STT`, 'info');
          // is_final:true endpoint for the finished utterance (both modes).
          liveCallRef.current?.sendAudioChunk(b64, fmt, pcm16);
        },
        ...(streaming
          ? {
              onFrame: (pcm16B64: string, sampleRate: number) =>
                liveCallRef.current?.sendAudioFrame(pcm16B64, sampleRate),
            }
          : {}),
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
      logEvent(
        streaming
          ? 'Microphone armed (streaming) — PCM frames forwarded continuously; VAD marks end-of-utterance'
          : 'Microphone armed — speak; VAD endpoints each utterance',
        'ok',
      );
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
    setEvents(prev => {
      const next = [...prev, { id, t: formatTime(durationRef.current), label, level }];
      eventsRef.current = next;  // keep the ref in sync so endCall (stale closure) persists them
      return next;
    });
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
    setPartialTranscript('');
    setEvents([]); eventsRef.current = [];
    setDuration(0);
    setCallState('connecting');
    logEvent('WebSocket handshake — short-lived JWT presented, upgrading…');

    try {
      const result = await apiStartSession(token!, bot.backendId!, bot.backendCustomerId ?? null);
      liveCrmRef.current = result.crm_context || {};
      setSessionId(result.session_id);
      logEvent(`Session ${result.session_id.slice(0, 8)} created · CRM record loaded · use_case=${result.use_case}`, 'ok');

      const handlers: LiveCallHandlers = {
        onOpen: () => logEvent('WebSocket upgraded — JWT accepted by VB_Svc', 'ok'),
        onCallState: (status) => setCallState(status as CallState),
        onTranscript: (speaker, text, d) => {
          // The authoritative turn transcript supersedes any live interim caption.
          setPartialTranscript('');
          addMsg({ kind: speaker === 'bot' ? 'bot' : 'user', text, tone: d.tone as Tone | undefined });
        },
        // Best-effort interim caption for the customer's in-progress utterance.
        onPartialTranscript: (speaker, text) => {
          if (speaker === 'customer') setPartialTranscript(text || '');
        },
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
    setEvents([]); eventsRef.current = [];
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
    setPartialTranscript('');
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
      events: [...eventsRef.current], // Item 35 — ref, not stale closure `events`
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


  return {
    scenario,
    customer,
    liveCall,
    callState,
    transcript,
    partialTranscript,
    events,
    duration,
    expandedInteraction,
    setExpandedInteraction,
    noInputCountdown,
    graceCountdown,
    sessionId,
    soundEnabled,
    setSoundEnabled,
    transcriptEndRef,
    eventsEndRef,
    eventsContainerRef,
    analyserRef,
    micOn,
    toggleMic,
    demoStepTooltip,
    runGuidedDemo,
    isLive,
    startCall,
    endCall,
    simulateNoReconnect,
    handleChip,
    handleBargeIn,
    handleBackchannel,
    handleUnsupported,
    handleLanguageSwitch,
    handleBackgroundNoise,
    handleNetworkDrop,
  };
}
