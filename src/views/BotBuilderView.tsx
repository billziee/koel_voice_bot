import React, { useState, useEffect } from 'react';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';

import type { BotConfig, SttMode, Flow, ConnectionSelection } from '../types';
import type { Theme } from '../lib/theme';
import { generateFlow, getKnowledgeIndexes, type KnowledgeIndex } from '../lib/koel';
import { KV } from '../components/KV';
import { SttModeSelector } from '../components/SttModeSelector';
import { ModelConnectionSelectors } from '../components/connections/ModelConnectionSelectors';
import { useConnections } from '../hooks/useConnections';
import { FlowEditor, defaultFlow, validateFlow } from '../components/FlowEditor';

// Single source of truth for which prompt variables the CRM context fills at call
// time, and the CRM field each resolves from. Drives BOTH the Step-4 mapping table
// and the preflight check, so they can never drift. A prompt token outside this
// map has no data source and would render literally on the call.
const CRM_FIELD_MAP: Record<string, string> = {
  customer_name: 'crm.full_name',
  name: 'crm.full_name',
  bank: 'crm.bank',
  product: 'crm.offer.product',
  product_name: 'crm.offer.product',
  offer: 'crm.offer.product',
  offer_amount: 'crm.offer.amount',
  benefit_1: 'crm.offer.benefit_primary',
  benefit_2: 'crm.offer.benefit_secondary',
  fee_clause: 'crm.offer.fee_waiver',
};

type BuilderStep = 1 | 2 | 3 | 4 | 5;
// Knowledge & CRM comes BEFORE Conversation flow: the flow step's "auto-build
// from knowledge base" button needs a RAG index already set (koel/nlp/flow_gen
// via generateFlow), so the index must be picked first or the button never
// appears on a first linear pass through the wizard (confirmed live — the
// index only existed on the OLD step 4, one step after the button that needed it).
const BUILDER_STEPS: { num: BuilderStep; title: string; sub: string }[] = [
  { num: 1, title: 'Use case', sub: 'Template & languages' },
  { num: 2, title: 'Persona & voice', sub: 'Name, gender, speed' },
  { num: 3, title: 'Knowledge & CRM', sub: 'RAG index · field map' },
  { num: 4, title: 'Conversation flow', sub: 'Script nodes & branches' },
  { num: 5, title: 'Review & deploy', sub: 'Preflight checks' },
];

export function BotBuilderView({ onDeploy, onCancel, theme, token }: { onDeploy: (config: BotConfig) => void; onCancel: () => void; theme: Theme; token: string | null }) {
  const [step, setStep] = useState<BuilderStep>(1);
  const [botName, setBotName] = useState('');
  const [useCase, setUseCase] = useState('Credit Card Cross-Sell');
  const [languages, setLanguages] = useState<string[]>(['English', 'Hindi', 'Hinglish']);
  const [voiceGender, setVoiceGender] = useState('Female');
  const [voiceSpeed, setVoiceSpeed] = useState(1.05);
  const [disclosure, setDisclosure] = useState(true);
  const [ambientEnabled, setAmbientEnabled] = useState(true);
  const [sttMode, setSttMode] = useState<SttMode>('chunk'); // Chunk = default
  // Per-bot BYOK model selection (Phase 4). Available list fetched via the token;
  // degrades to Default-only when the backend is offline or the fetch fails.
  const availableConnections = useConnections(token);
  const [connectionSel, setConnectionSel] = useState<ConnectionSelection>({});
  const [ragIndex, setRagIndex] = useState('');
  // Real scripted flow (6.6): seeded with a minimal single-node starter, edited
  // in place, and shipped in config.flow on deploy.
  const [flow, setFlow] = useState<Flow>(() => defaultFlow());
  // Live knowledge indexes for the RAG picker (Step 3). Fetched with the token;
  // degrades to an empty list (plain free-text entry still works) when offline.
  const [knowledgeIndexes, setKnowledgeIndexes] = useState<KnowledgeIndex[]>([]);
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    getKnowledgeIndexes(token)
      .then((ix) => { if (!cancelled) setKnowledgeIndexes(ix); })
      .catch(() => { /* offline/demo → picker stays empty, free-text entry still works */ });
    return () => { cancelled = true; };
  }, [token]);

  // Flow shape stats + validation feed the footer, review panel, and preflight.
  const flowNodeCount = Object.keys(flow.nodes).length;
  const flowBranchCount = Object.values(flow.nodes).reduce((n, node) => n + Object.keys(node.branches).length, 0);
  const flowErrors = validateFlow(flow).filter((i) => i.level === 'error').length;

  // The UNIQUE variables actually used across ALL node prompts (real flow, not a
  // hardcoded list), in first-seen order. A token outside CRM_FIELD_MAP has no
  // data source, so it would render literally on the call.
  const flowPromptText = Object.values(flow.nodes).map((n) => n.prompt).join(' ');
  const scriptVars = Array.from(
    new Set(Array.from(flowPromptText.matchAll(/\{([^}]+)\}/g), (m) => m[1].trim())),
  );
  const unmappedVars = scriptVars.filter((v) => !(v in CRM_FIELD_MAP));

  // Real preflight (Q-3): each row reflects the builder's ACTUAL state rather
  // than a hardcoded pass. Variable-mapping, flow validity, and index checks are
  // deterministic client-side; a live index-reachability probe is a deploy-time
  // follow-up.
  const preflight = [
    {
      label:
        flowErrors === 0
          ? 'Conversation flow is valid'
          : `Conversation flow has ${flowErrors} error${flowErrors === 1 ? '' : 's'}`,
      ok: flowErrors === 0,
    },
    {
      label:
        unmappedVars.length === 0
          ? 'All prompt variables mapped to CRM'
          : `Unmapped prompt variables: ${unmappedVars.map((v) => `{${v}}`).join(', ')}`,
      ok: unmappedVars.length === 0,
    },
    { label: 'Knowledge base index configured', ok: ragIndex.trim().length > 0 },
    { label: 'Compliance disclosure block present', ok: disclosure },
    { label: 'Call-hours window not yet set (RBI)', ok: false },
  ];

  // Resolve a selected connection id to its display name for the review panel;
  // an unset id reads as the built-in default.
  const connName = (id?: string): string => {
    if (!id) return 'Default';
    return availableConnections.find((c) => c.id === id)?.name ?? id;
  };

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
      sttMode,
      flow,
      connections: connectionSel,
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
              {step === 3 && 'Connect the RAG knowledge base and map CRM fields to script variables.'}
              {step === 4 && 'Define how KOEL moves through the call. Each node is a script step; branches are driven by intent classification.'}
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
                <SttModeSelector value={sttMode} onChange={setSttMode} theme={theme} />
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Model connections</label>
                  <ModelConnectionSelectors connections={availableConnections} value={connectionSel} onChange={setConnectionSel} theme={theme} />
                  <p className="text-[11px] mt-1.5" style={{ color: theme.faint }}>Pick a BYOK provider per stage, or leave on Default (built-in). Add providers under Model Connections.</p>
                </div>
              </div>
            )}

            {/* Step 3: Knowledge & CRM — wired to live indexes + the real flow.
                Comes BEFORE Conversation flow so the RAG index below is already
                set by the time the auto-build button (step 4) needs it. */}
            {step === 3 && (
              <div className="space-y-5">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>RAG knowledge index</label>
                  <input
                    list="kb-indexes"
                    value={ragIndex}
                    onChange={e => setRagIndex(e.target.value)}
                    placeholder={knowledgeIndexes.length ? 'Pick an existing index or type a new one' : 'e.g. cc_hdfc'}
                    className="w-full border rounded-md px-3 py-2 text-[13px] font-mono outline-none focus:ring-2"
                    style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink }}
                  />
                  <datalist id="kb-indexes">
                    {knowledgeIndexes.map((ix) => (
                      <option key={ix.rag_index} value={ix.rag_index}>{`${ix.chunk_count} chunk${ix.chunk_count === 1 ? '' : 's'}`}</option>
                    ))}
                  </datalist>
                  <p className="text-[11px] mt-1.5" style={{ color: theme.faint }}>
                    {(() => {
                      const sel = knowledgeIndexes.find((ix) => ix.rag_index === ragIndex);
                      if (sel) return `“${sel.rag_index}” · ${sel.chunk_count} chunk${sel.chunk_count === 1 ? '' : 's'} embedded and retrievable.`;
                      if (knowledgeIndexes.length) return `${knowledgeIndexes.length} index${knowledgeIndexes.length === 1 ? '' : 'es'} available. A new name starts an empty index — add content under Knowledge Base.`;
                      return token
                        ? 'No indexes found yet — create one under Knowledge Base, or type a name to start fresh.'
                        : 'Connect to a live backend to list existing indexes.';
                    })()}
                  </p>
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-2" style={{ color: theme.ink2 }}>
                    CRM variable mapping
                    <span className="ml-2 font-normal" style={{ color: theme.faint }}>· derived from your conversation flow</span>
                  </label>
                  {scriptVars.length === 0 ? (
                    <div className="rounded-md border px-4 py-6 text-center text-[12.5px]" style={{ borderColor: theme.rule, color: theme.faint }}>
                      No <span className="font-mono">{'{variables}'}</span> used in your flow yet. Add a token like{' '}
                      <span className="font-mono" style={{ color: theme.primary }}>{'{customer_name}'}</span> to a prompt in the Conversation flow step and it’ll appear here.
                    </div>
                  ) : (
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
                          {scriptVars.map((v) => {
                            const field = CRM_FIELD_MAP[v];
                            const mapped = field !== undefined;
                            return (
                              <tr key={v} className="border-t" style={{ borderColor: theme.ruleSubtle }}>
                                <td className="px-3 py-2 font-mono" style={{ color: theme.primary }}>{`{${v}}`}</td>
                                <td className="px-3 py-2 font-mono" style={{ color: mapped ? theme.ink : theme.faint }}>{field ?? '—'}</td>
                                <td className="px-3 py-2">
                                  <span className="px-2 py-0.5 rounded text-[11px] font-medium" style={{ backgroundColor: mapped ? theme.greenLight : theme.warningLight, color: mapped ? theme.green : theme.warning }}>
                                    {mapped ? 'Mapped' : 'Unmapped'}
                                  </span>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {unmappedVars.length > 0 && (
                    <p className="text-[11px] mt-2" style={{ color: theme.warning }}>
                      {unmappedVars.length} unmapped variable{unmappedVars.length === 1 ? '' : 's'} — no CRM source, so it renders literally on the call. Rename it to a supported field or remove it from the flow.
                    </p>
                  )}
                </div>
              </div>
            )}

            {/* Step 4: Conversation flow — real editor (canvas + form). RAG index
                is already set from step 3, so the auto-build button below works
                on the very first linear pass through the wizard. */}
            {step === 4 && (
              <FlowEditor
                value={flow}
                onChange={setFlow}
                theme={theme}
                onAutoBuild={
                  token && ragIndex.trim()
                    ? () => generateFlow(token, { ragIndex, useCase, language: languages[0] })
                    : undefined
                }
              />
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
                  <KV k="STT mode" v={sttMode === 'ws' ? 'WebSocket streaming' : 'Chunk (batch)'} theme={theme} />
                  <KV k="LLM model" v={connName(connectionSel.llm)} theme={theme} />
                  <KV k="TTS voice" v={connName(connectionSel.tts)} theme={theme} />
                  <KV k="STT model" v={connName(connectionSel.stt)} theme={theme} />
                </ReviewBlock>
                <ReviewBlock title="Flow & knowledge" theme={theme}>
                  <KV k="Flow nodes" v={`${flowNodeCount}`} theme={theme} />
                  <KV k="Branches" v={`${flowBranchCount}`} theme={theme} />
                  <KV k="Start node" v={flow.start || '—'} theme={theme} mono />
                  <KV k="RAG index" v={ragIndex} theme={theme} mono />
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
              {step === 4 && `${flowNodeCount} node${flowNodeCount === 1 ? '' : 's'} · ${flowBranchCount} branch${flowBranchCount === 1 ? '' : 'es'} · ${flowErrors} error${flowErrors === 1 ? '' : 's'}`}
              {step === 5 && (() => {
                const warnings = preflight.filter(p => !p.ok).length;
                return `${preflight.filter(p => p.ok).length} of ${preflight.length} checks passed · ${warnings} warning${warnings === 1 ? '' : 's'}`;
              })()}
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

