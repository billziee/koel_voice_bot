import React, { useState } from 'react';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';

import type { BotConfig } from '../types';
import type { Theme } from '../lib/theme';
import { KV } from '../components/KV';

type BuilderStep = 1 | 2 | 3 | 4 | 5;
const BUILDER_STEPS: { num: BuilderStep; title: string; sub: string }[] = [
  { num: 1, title: 'Use case', sub: 'Template & languages' },
  { num: 2, title: 'Persona & voice', sub: 'Name, gender, speed' },
  { num: 3, title: 'Conversation flow', sub: 'Script nodes & branches' },
  { num: 4, title: 'Knowledge & CRM', sub: 'RAG index · field map' },
  { num: 5, title: 'Review & deploy', sub: 'Preflight checks' },
];

const FLOW_NODES = [
  { type: 'greet', label: 'Greeting', title: 'Personalised opener', detail: 'CRM: name, bank, product, offer' },
  { type: 'listen', label: 'Listen', title: 'Capture & classify intent', detail: 'STT → Groq intent tag' },
];

const FLOW_BRANCHES = [
  { type: 'rag', label: 'RAG response', title: 'Pitch benefits', detail: 'on intent = INTERESTED' },
  { type: 'rag', label: 'RAG response', title: 'Handle objection', detail: 'on intent = OBJECTION' },
  { type: 'escalate', label: 'Escalate', title: 'Hand to specialist', detail: 'on intent = HIGH_INTEREST' },
  { type: 'end', label: 'End', title: 'Log & close', detail: 'on intent = REJECTION · suppress 30d' },
];

export function BotBuilderView({ onDeploy, onCancel, theme }: { onDeploy: (config: BotConfig) => void; onCancel: () => void; theme: Theme }) {
  const [step, setStep] = useState<BuilderStep>(1);
  const [botName, setBotName] = useState('');
  const [useCase, setUseCase] = useState('Credit Card Cross-Sell');
  const [languages, setLanguages] = useState<string[]>(['English', 'Hindi', 'Hinglish']);
  const [voiceGender, setVoiceGender] = useState('Female');
  const [voiceSpeed, setVoiceSpeed] = useState(1.05);
  const [disclosure, setDisclosure] = useState(true);
  const [ambientEnabled, setAmbientEnabled] = useState(true);
  const [ragIndex, setRagIndex] = useState('kb_hdfc_regalia_v3');
  const [selectedNode, setSelectedNode] = useState<string | null>('Handle objection');
  const [nodeScript, setNodeScript] = useState('Bilkul samajh sakti hoon. Yeh card {benefit_1} aur {benefit_2} deta hai — aapke existing cards se alag benefit. {fee_clause}. Interested hain?');

  const nodeColor = (type: string) => {
    switch (type) {
      case 'greet': return theme.primary;
      case 'listen': return theme.faint;
      case 'rag': return theme.greenMid;
      case 'escalate': return theme.accent;
      case 'end': return theme.destructive;
      default: return theme.faint;
    }
  };

  const preflight = [
    { label: 'All script variables mapped to CRM', ok: true },
    { label: 'Knowledge base index reachable', ok: true },
    { label: 'Compliance disclosure block present', ok: disclosure },
    { label: 'Call-hours window not yet set (RBI)', ok: false },
  ];

  const handleDeploy = () => {
    onDeploy({
      name: botName || 'KOEL — New Bot',
      useCase,
      language: languages.join(', '),
      persona: `KOEL · ${voiceGender} · ${voiceSpeed}×`,
      speed: voiceSpeed,
      ambientVolume: 20,
      ambientProfile: ambientEnabled ? 'callfloor' : 'silent',
      backchannels: ['hmm', 'haan', 'okay'],
      ragEnabled: true,
      ragIndex,
      greeting: '',
      tone: 'cheerful',
    });
  };

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>
            {step === 5 ? 'Review & deploy' : 'Build a new bot'}
          </h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>
            Step {step} of 5 · {BUILDER_STEPS[step - 1].title}{step > 1 ? ` · ${useCase}` : ''}
          </p>
        </div>
        <button onClick={onCancel} className="text-[13px] font-medium px-3 py-1.5 rounded-md hover:opacity-80" style={{ color: theme.inkMuted }}>Cancel</button>
      </div>

      <div className="grid gap-6" style={{ gridTemplateColumns: '200px 1fr' }}>
        {/* Stepper */}
        <div className="flex flex-col gap-0">
          {BUILDER_STEPS.map((s, i) => (
            <React.Fragment key={s.num}>
              <button
                onClick={() => s.num < step && setStep(s.num)}
                className="flex items-start gap-3 text-left py-2 px-2 rounded-md"
                style={{ cursor: s.num < step ? 'pointer' : s.num === step ? 'default' : 'default', opacity: s.num > step ? 0.5 : 1 }}
              >
                <span
                  className="w-[22px] h-[22px] rounded-full flex items-center justify-center text-[11px] font-bold shrink-0 mt-0.5"
                  style={{
                    backgroundColor: s.num < step ? theme.green : s.num === step ? theme.primary : theme.ruleSubtle,
                    color: s.num <= step ? '#fff' : theme.faint,
                  }}
                >
                  {s.num < step ? '✓' : s.num}
                </span>
                <span>
                  <span className="block text-[13px] font-medium" style={{ color: s.num === step ? theme.ink : theme.inkMuted }}>{s.title}</span>
                  {s.num <= step && <span className="block text-[11.5px]" style={{ color: theme.faint }}>{s.sub}</span>}
                </span>
              </button>
              {i < BUILDER_STEPS.length - 1 && (
                <div className="w-px h-3 ml-[12px]" style={{ backgroundColor: s.num < step ? theme.green : theme.ruleSubtle }} />
              )}
            </React.Fragment>
          ))}
        </div>

        {/* Main panel */}
        <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="p-5 border-b" style={{ borderColor: theme.ruleSubtle }}>
            <h2 className="text-[15px] font-semibold" style={{ color: theme.ink }}>{BUILDER_STEPS[step - 1].title}</h2>
            <p className="text-[12.5px] mt-1" style={{ color: theme.faint }}>
              {step === 1 && 'Select the use case template and supported languages.'}
              {step === 2 && 'Configure the bot persona, voice, and speech settings.'}
              {step === 3 && 'Define how KOEL moves through the call. Each node is a script step; branches are driven by intent classification.'}
              {step === 4 && 'Connect the RAG knowledge base and map CRM fields to script variables.'}
              {step === 5 && 'Review the configuration below. Once deployed, the bot appears on the dashboard.'}
            </p>
          </div>

          <div className="p-5">
            {/* Step 1: Use case */}
            {step === 1 && (
              <div className="space-y-5">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Bot display name</label>
                  <input value={botName} onChange={e => setBotName(e.target.value)} placeholder="KOEL — HDFC Regalia" className="w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} />
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Use case template</label>
                  <select value={useCase} onChange={e => setUseCase(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}>
                    <option>Credit Card Cross-Sell</option>
                    <option>Collections Pre-Due</option>
                    <option>Personal Loan Sell</option>
                    <option>Life Insurance Sell</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Languages</label>
                  <div className="flex flex-wrap gap-2">
                    {['English', 'Hindi', 'Hinglish', 'Tamil', 'Telugu', 'Marathi'].map(lang => (
                      <button key={lang} onClick={() => setLanguages(prev => prev.includes(lang) ? prev.filter(l => l !== lang) : [...prev, lang])} className="px-3 py-1.5 rounded-md text-[12.5px] font-medium border" style={{ borderColor: languages.includes(lang) ? theme.primary : theme.rule, backgroundColor: languages.includes(lang) ? theme.primaryLight : 'transparent', color: languages.includes(lang) ? theme.primary : theme.inkMuted }}>
                        {lang}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* Step 2: Persona & voice */}
            {step === 2 && (
              <div className="space-y-5">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Voice gender</label>
                    <select value={voiceGender} onChange={e => setVoiceGender(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px]" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}>
                      <option>Female</option>
                      <option>Male</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Speed</label>
                    <div className="flex items-center gap-3">
                      <input type="range" min={0.8} max={1.3} step={0.05} value={voiceSpeed} onChange={e => setVoiceSpeed(parseFloat(e.target.value))} className="flex-1" />
                      <span className="text-[13px] font-mono w-12 text-right" style={{ color: theme.ink }}>{voiceSpeed.toFixed(2)}×</span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center justify-between py-3 px-4 rounded-md border" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset }}>
                  <div>
                    <span className="text-[13px] font-medium" style={{ color: theme.ink }}>Compliance disclosure block</span>
                    <p className="text-[11.5px]" style={{ color: theme.faint }}>RBI-mandated disclosure played at start of call</p>
                  </div>
                  <button onClick={() => setDisclosure(!disclosure)} className="w-9 h-5 rounded-full relative transition-colors" style={{ backgroundColor: disclosure ? theme.primary : theme.rule }}>
                    <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all" style={{ left: disclosure ? '18px' : '2px' }} />
                  </button>
                </div>
                <div className="flex items-center justify-between py-3 px-4 rounded-md border" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset }}>
                  <div>
                    <span className="text-[13px] font-medium" style={{ color: theme.ink }}>Ambient call-floor audio</span>
                    <p className="text-[11.5px]" style={{ color: theme.faint }}>Background chatter under the bot's voice — only while it speaks, never in silence</p>
                  </div>
                  <button onClick={() => setAmbientEnabled(!ambientEnabled)} className="w-9 h-5 rounded-full relative transition-colors" style={{ backgroundColor: ambientEnabled ? theme.primary : theme.rule }}>
                    <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all" style={{ left: ambientEnabled ? '18px' : '2px' }} />
                  </button>
                </div>
              </div>
            )}

            {/* Step 3: Conversation flow */}
            {step === 3 && (
              <div>
                <div className="flex items-center justify-between mb-4">
                  <div className="flex gap-4 text-[11.5px]" style={{ color: theme.faint }}>
                    {[{ c: theme.primary, l: 'Greeting' }, { c: theme.faint, l: 'Listen' }, { c: theme.greenMid, l: 'RAG response' }, { c: theme.accent, l: 'Escalate' }, { c: theme.destructive, l: 'End' }].map(lg => (
                      <span key={lg.l} className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: lg.c }} />{lg.l}</span>
                    ))}
                  </div>
                  <button className="text-[12.5px] font-medium px-3 py-1 border rounded-md" style={{ borderColor: theme.rule, color: theme.inkMuted }}>Test flow</button>
                </div>

                <div className="p-4 rounded-lg" style={{ backgroundColor: theme.inset }}>
                  {/* Linear nodes */}
                  {FLOW_NODES.map((n, i) => (
                    <React.Fragment key={i}>
                      <div className="p-3 rounded-md border cursor-pointer" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.surfaceCard }} onClick={() => setSelectedNode(n.title)}>
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider mb-1" style={{ color: nodeColor(n.type) }}>
                          <span className="w-[5px] h-[5px] rounded-full" style={{ backgroundColor: nodeColor(n.type) }} />{n.label}
                        </span>
                        <div className="text-[13px] font-medium" style={{ color: theme.ink }}>{n.title}</div>
                        <div className="text-[11.5px]" style={{ color: theme.faint }}>{n.detail}</div>
                      </div>
                      <div className="w-px h-4 mx-auto" style={{ backgroundColor: theme.rule }} />
                    </React.Fragment>
                  ))}

                  {/* Branches */}
                  <div className="grid grid-cols-4 gap-3 mt-1">
                    {FLOW_BRANCHES.map((b, i) => (
                      <div key={i} className="p-3 rounded-md border cursor-pointer" style={{ borderColor: selectedNode === b.title ? theme.primary : theme.ruleSubtle, backgroundColor: selectedNode === b.title ? theme.primarySubtle : theme.surfaceCard }} onClick={() => { setSelectedNode(b.title); setNodeScript(b.detail); }}>
                        <span className="inline-flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider mb-1" style={{ color: nodeColor(b.type) }}>
                          <span className="w-[5px] h-[5px] rounded-full" style={{ backgroundColor: nodeColor(b.type) }} />{b.label}
                        </span>
                        <div className="text-[12.5px] font-medium" style={{ color: theme.ink }}>{b.title}</div>
                        <div className="text-[11px]" style={{ color: theme.faint }}>{b.detail}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {selectedNode && (
                  <div className="flex gap-4 mt-4">
                    <div className="flex-1">
                      <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Selected node — {selectedNode}</label>
                      <textarea value={nodeScript} onChange={e => setNodeScript(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px] outline-none min-h-[64px] resize-y" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} />
                    </div>
                    <div className="w-[200px]">
                      <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Knowledge source</label>
                      <select value={ragIndex} onChange={e => setRagIndex(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px]" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}>
                        <option>kb_hdfc_regalia_v3</option>
                        <option>kb_hdfc_millennia_v1</option>
                        <option>kb_icici_plat_v2</option>
                      </select>
                      <p className="text-[11px] mt-2" style={{ color: theme.faint }}>Retrieved per utterance — no hallucinated product details.</p>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Step 4: Knowledge & CRM */}
            {step === 4 && (
              <div className="space-y-5">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>RAG knowledge index</label>
                  <input value={ragIndex} onChange={e => setRagIndex(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px] font-mono" style={{ borderColor: theme.rule, color: theme.ink }} />
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-3" style={{ color: theme.ink2 }}>CRM variable mapping</label>
                  <div className="rounded-md border overflow-hidden" style={{ borderColor: theme.rule }}>
                    <table className="w-full text-[12.5px]">
                      <thead>
                        <tr style={{ backgroundColor: theme.inset }}>
                          <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>Script variable</th>
                          <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>CRM field</th>
                          <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[
                          { variable: '{customer_name}', field: 'crm.full_name', mapped: true },
                          { variable: '{product_name}', field: 'crm.offer.product', mapped: true },
                          { variable: '{benefit_1}', field: 'crm.offer.benefit_primary', mapped: true },
                          { variable: '{benefit_2}', field: 'crm.offer.benefit_secondary', mapped: true },
                          { variable: '{fee_clause}', field: 'crm.offer.fee_waiver', mapped: true },
                          { variable: '{pre_approved_limit}', field: '—', mapped: false },
                        ].map((row, i) => (
                          <tr key={i} className="border-t" style={{ borderColor: theme.ruleSubtle }}>
                            <td className="px-3 py-2 font-mono" style={{ color: theme.primary }}>{row.variable}</td>
                            <td className="px-3 py-2 font-mono" style={{ color: theme.ink }}>{row.field}</td>
                            <td className="px-3 py-2">
                              <span className="px-2 py-0.5 rounded text-[11px] font-medium" style={{ backgroundColor: row.mapped ? theme.greenLight : theme.warningLight, color: row.mapped ? theme.green : theme.warning }}>
                                {row.mapped ? 'Mapped' : 'Unmapped'}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            )}

            {/* Step 5: Review */}
            {step === 5 && (
              <div className="grid grid-cols-2 gap-4">
                <ReviewBlock title="Identity" theme={theme}>
                  <KV k="Display name" v={botName || 'KOEL — New Bot'} theme={theme} />
                  <KV k="Use case" v={useCase} theme={theme} />
                  <KV k="Direction" v="Outgoing" theme={theme} />
                  <KV k="Languages" v={languages.join(' · ')} theme={theme} />
                </ReviewBlock>
                <ReviewBlock title="Voice" theme={theme}>
                  <KV k="Voice" v={voiceGender} theme={theme} />
                  <KV k="Speed" v={`${voiceSpeed.toFixed(2)}×`} theme={theme} />
                  <KV k="Greeting" v="Personalised template" theme={theme} />
                  <KV k="Disclosure block" v={disclosure ? 'Enabled' : 'Disabled'} theme={theme} />
                </ReviewBlock>
                <ReviewBlock title="Flow & knowledge" theme={theme}>
                  <KV k="Script nodes" v={`${FLOW_NODES.length + FLOW_BRANCHES.length}`} theme={theme} />
                  <KV k="Branches" v={`${FLOW_BRANCHES.length}`} theme={theme} />
                  <KV k="RAG index" v={ragIndex} theme={theme} mono />
                  <KV k="Escalation rules" v="4" theme={theme} />
                </ReviewBlock>
                <ReviewBlock title="Preflight checks" theme={theme}>
                  {preflight.map((pf, i) => (
                    <div key={i} className="flex items-start gap-2 text-[12.5px] py-1">
                      {pf.ok ? (
                        <CheckCircle2 size={14} style={{ color: theme.green, flexShrink: 0, marginTop: 1 }} />
                      ) : (
                        <AlertTriangle size={14} style={{ color: theme.warning, flexShrink: 0, marginTop: 1 }} />
                      )}
                      <span style={{ color: pf.ok ? theme.ink : theme.warning }}>{pf.label}</span>
                    </div>
                  ))}
                </ReviewBlock>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between p-4 border-t" style={{ borderColor: theme.ruleSubtle }}>
            <button onClick={() => step > 1 ? setStep((step - 1) as BuilderStep) : onCancel()} className="text-[13px] font-medium px-4 py-2 border rounded-md" style={{ borderColor: theme.rule, color: theme.inkMuted }}>
              ← Back
            </button>
            <span className="text-[12px]" style={{ color: theme.faint }}>
              {step === 3 && `${FLOW_BRANCHES.length} branches · ${FLOW_NODES.length + FLOW_BRANCHES.length} nodes · 0 unmapped variables`}
              {step === 5 && `${preflight.filter(p => p.ok).length} of ${preflight.length} checks passed · ${preflight.filter(p => !p.ok).length} warning`}
            </span>
            {step < 5 ? (
              <button onClick={() => setStep((step + 1) as BuilderStep)} className="text-[13px] font-bold px-5 py-2 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
                Continue →
              </button>
            ) : (
              <button onClick={handleDeploy} className="text-[13px] font-bold px-5 py-2 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
                Deploy bot
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function ReviewBlock({ title, children, theme }: { title: string; children: React.ReactNode; theme: Theme }) {
  return (
    <div className="rounded-md border p-4" style={{ borderColor: theme.ruleSubtle }}>
      <div className="text-[12px] font-bold uppercase tracking-wider mb-3" style={{ color: theme.faint }}>{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

