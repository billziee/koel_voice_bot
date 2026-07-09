import React, { useState, useEffect, useRef } from 'react';
import {
  Square, Mic, PhoneCall, User, Database, Activity, ShieldAlert, Zap,
  MessageSquare, ChevronRight, ChevronDown, CheckCircle2, AlertCircle,
  WifiOff, Languages, Timer, Volume2, Radio,
} from 'lucide-react';
import { micSupported } from '../lib/audio';
import type { CallState, Bot, CallHistory } from '../types';
import type { Theme } from '../lib/theme';
import { formatTime } from '../lib/format';
import { TranscriptMessage } from '../components/TranscriptMessage';
import { PAST_INTERACTIONS } from '../data/callScenarios';
import { useLiveCall } from '../hooks/useLiveCall';

// Screen 3 — Active Call Sandbox (presentational). The call state machine lives
// in useLiveCall; this file owns layout + the call-only sub-components.

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
  const {
    scenario,
    customer,
    liveCall,
    callState,
    transcript,
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
  } = useLiveCall({ bot, onCallCompleted, live, token });

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
