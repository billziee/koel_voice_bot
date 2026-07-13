import { useState } from 'react';
import { Lock } from 'lucide-react';

import type { Bot, BotConfig, AmbientProfile, Tone, UseCase, SttMode, Flow } from '../types';
import type { Theme } from '../lib/theme';
import { KV } from '../components/KV';
import { SttModeSelector } from '../components/SttModeSelector';
import { FlowEditor, defaultFlow } from '../components/FlowEditor';

// Default config seed per use case (ConfigView-only).
const DEFAULT_CONFIGS: Record<UseCase, BotConfig> = {
  'Credit Card': { name: 'HDFC Card Cross-Sell', useCase: 'Credit Card Cross-Sell', language: 'Hinglish', persona: 'Kore — Female, Firm & Professional', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah'], ragEnabled: true, ragIndex: 'hdfc_regalia_benefits_v2', greeting: 'Namaste {customer.name} ji, main {bank} ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved {offer.product} offer hai. Do minute baat kar sakte hain?', tone: 'cheerful', sttMode: 'chunk' },
  'Collections': { name: 'ICICI Pre-Due Collections', useCase: 'Collections — Pre-Due (DPD 0–30)', language: 'Hindi', persona: 'Kore — Female, Firm & Professional', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah'], ragEnabled: true, ragIndex: 'icici_collection_guidelines_2026', greeting: 'Namaste {customer.name} ji, main {bank} ki taraf se KOEL bol rahi hoon — yeh ek automated reminder call hai.', tone: 'firm', sttMode: 'chunk' },
  'Insurance': { name: 'MaxLife Term Pitch', useCase: 'Life Insurance Sell (IRDAI flow)', language: 'English', persona: 'Zephyr — Female, Bright & Cheerful', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'okay', 'yeah', 'right', 'I see'], ragEnabled: true, ragIndex: 'maxlife_term_disclosures', greeting: 'Hello {customer.name} sir, this is KOEL calling from MaxLife — this is an automated call.', tone: 'cheerful', sttMode: 'chunk' },
  'Personal Loan': { name: 'Axis Loan Origination', useCase: 'Personal Loan Sell', language: 'Hinglish', persona: 'Zephyr — Female, Bright & Cheerful', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah'], ragEnabled: true, ragIndex: 'axis_pl_rate_card_q2', greeting: 'Namaste {customer.name} ji, main {bank} ki taraf se KOEL bol rahi hoon — yeh ek automated call hai.', tone: 'cheerful', sttMode: 'chunk' },
};

interface ConfigViewProps {
  bot?: Bot;
  existingConfig?: BotConfig;
  isNew: boolean;
  onSave: (config: BotConfig) => void;
  onCancel: () => void;
  theme: Theme;
}

export function ConfigView({ bot, existingConfig, isNew, onSave, onCancel, theme }: ConfigViewProps) {
  // Seed from existing config > bot defaults > empty
  const seed = existingConfig || (bot ? DEFAULT_CONFIGS[bot.useCase] : undefined);

  const [name, setName] = useState(seed?.name || '');
  const [useCase, setUseCase] = useState(seed?.useCase || 'Credit Card Cross-Sell');
  const [language, setLanguage] = useState(seed?.language || 'Hinglish');
  const [persona, setPersona] = useState(seed?.persona || 'Kore — Female, Firm & Professional');
  const [speed, setSpeed] = useState(seed?.speed || 1.0);
  const [ambientVolume] = useState(seed?.ambientVolume || 20);
  const [ambientProfile, setAmbientProfile] = useState<AmbientProfile>(seed?.ambientProfile || 'callfloor'); // Item 7
  const [backchannels] = useState(seed?.backchannels || ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah']);
  const [ragEnabled, setRagEnabled] = useState(seed?.ragEnabled ?? true);
  const [ragIndex, setRagIndex] = useState(seed?.ragIndex || 'hdfc_regalia_benefits_v2');
  const [greeting, setGreeting] = useState(seed?.greeting || '');
  const [tone] = useState<Tone>(seed?.tone || 'cheerful');
  // Seed priority: config saved this session > backend-persisted config.stt_mode
  // (carried on the Bot) > default. Chunk is the default when nothing is set.
  const initialSttMode: SttMode = existingConfig?.sttMode ?? bot?.sttMode ?? 'chunk';
  const [sttMode, setSttMode] = useState<SttMode>(initialSttMode);
  // Scripted flow (6.6): seed priority mirrors sttMode — config saved this
  // session > backend-hydrated bot.flow > a minimal default starter.
  const seedFlow: Flow = existingConfig?.flow ?? bot?.flow ?? defaultFlow();
  const [flow, setFlow] = useState<Flow>(seedFlow);

  const handleSave = () => {
    onSave({ name, useCase, language, persona, speed, ambientVolume, ambientProfile, backchannels, ragEnabled, ragIndex, greeting, tone, sttMode, flow });
  };

  // Truthful status badge (Q-3): only claim "unsaved changes" when an editable
  // field actually diverges from what was seeded. Mirrors the useState defaults
  // so a freshly-opened, untouched form reads as clean. A brand-new bot is
  // inherently unsaved until the first save.
  const isDirty =
    name !== (seed?.name || '') ||
    useCase !== (seed?.useCase || 'Credit Card Cross-Sell') ||
    language !== (seed?.language || 'Hinglish') ||
    persona !== (seed?.persona || 'Kore — Female, Firm & Professional') ||
    speed !== (seed?.speed || 1.0) ||
    ambientProfile !== (seed?.ambientProfile || 'callfloor') ||
    ragEnabled !== (seed?.ragEnabled ?? true) ||
    ragIndex !== (seed?.ragIndex || 'hdfc_regalia_benefits_v2') ||
    greeting !== (seed?.greeting || '') ||
    sttMode !== initialSttMode ||
    JSON.stringify(flow) !== JSON.stringify(seedFlow);
  const showDraft = isNew || isDirty;

  const [disclosureEnabled, setDisclosureEnabled] = useState(true);

  const escalationRules = [
    { condition: 'intent = HIGH_INTEREST', target: 'Sales specialist queue', badge: 'Real-time', badgeColor: theme.primary },
    { condition: 'sentiment < −0.6', target: 'Senior agent queue', badge: 'Real-time', badgeColor: theme.primary },
    { condition: 'customer requests human', target: 'Any available agent', badge: 'Real-time', badgeColor: theme.primary },
    { condition: 'crm.dispute_flag = true', target: 'Do not call · suppress', badge: 'Compliance', badgeColor: theme.destructive },
  ];

  const crmMappings = [
    { variable: '{customer_name}', field: 'crm.profile.full_name', mapped: true },
    { variable: '{product_name}', field: 'crm.offer.product', mapped: true },
    { variable: '{offer_amount}', field: 'crm.offer.limit_inr', mapped: true },
    { variable: '{annual_fee}', field: '—', mapped: false },
  ];

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>
            Configure bot
          </h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>
            {bot?.name || name || 'HDFC Credit Card'} · {useCase || 'Cross-sell template'}
          </p>
        </div>
        {showDraft ? (
          <span className="px-3 py-1 rounded-md text-[12px] font-medium" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>
            {isNew ? 'New — unsaved' : 'Unsaved changes'}
          </span>
        ) : (
          <span className="px-3 py-1 rounded-md text-[12px] font-medium" style={{ backgroundColor: theme.ruleSubtle, color: theme.faint }}>Saved</span>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Left column */}
        <div className="flex flex-col gap-4">
          {/* Bot name & persona */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Bot name & persona</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>Identity used at call start, per RBI disclosure rules</div>
            </div>
            <div className="p-4 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Display name</label>
                  <input type="text" className="w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={name} onChange={e => setName(e.target.value)} />
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Voice</label>
                  <div className="flex border rounded-md overflow-hidden" style={{ borderColor: theme.rule }}>
                    {['Female', 'Male'].map(g => (
                      <button key={g} onClick={() => setPersona(g === 'Female' ? 'Kore — Female, Firm & Professional' : 'Charon — Male, Informative')} className="flex-1 py-2 text-[12.5px] font-medium" style={{ backgroundColor: persona.includes(g) ? theme.primary : 'transparent', color: persona.includes(g) ? '#fff' : theme.inkMuted }}>
                        {g}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
              <div>
                <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Speaking speed</label>
                <div className="flex items-center gap-3">
                  <span className="text-[11.5px]" style={{ color: theme.faint }}>Slower</span>
                  <input type="range" min={0.75} max={1.25} step={0.05} value={speed} onChange={e => setSpeed(parseFloat(e.target.value))} className="flex-1 accent-[#053BEE]" />
                  <span className="text-[11.5px]" style={{ color: theme.faint }}>Faster</span>
                  <span className="text-[12px] font-mono" style={{ color: theme.ink2 }}>{speed.toFixed(2)}×</span>
                </div>
              </div>
              <div>
                <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Greeting script template</label>
                <textarea className="w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2 min-h-[80px] resize-y" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={greeting} onChange={e => setGreeting(e.target.value)} placeholder="Namaste {customer_name} ji, main {bank_name} ki taraf se KOEL bol rahi hoon..." />
                <p className="text-[11px] mt-1" style={{ color: theme.faint }}>Variables in braces resolve from CRM at session start.</p>
              </div>
              <SttModeSelector value={sttMode} onChange={setSttMode} theme={theme} />
            </div>
          </div>

          {/* Use case mapping */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Use case mapping</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>Template + CRM field bindings for script variables</div>
            </div>
            <div className="p-4 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Use case template</label>
                  <select className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={useCase} onChange={e => setUseCase(e.target.value)}>
                    <option>Credit Card Cross-Sell</option>
                    <option>Collections — Pre-Due</option>
                    <option>Personal Loan Sell</option>
                    <option>Life Insurance Sell</option>
                    <option>Custom</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Languages</label>
                  <select className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={language} onChange={e => setLanguage(e.target.value)}>
                    <option>English · Hindi · Hinglish</option>
                    <option>English only</option>
                    <option>Hindi only</option>
                  </select>
                </div>
              </div>
              <div className="rounded-md border overflow-hidden" style={{ borderColor: theme.ruleSubtle }}>
                <table className="w-full text-[12.5px]">
                  <thead>
                    <tr style={{ backgroundColor: theme.inset }}>
                      <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>Script variable</th>
                      <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>CRM field</th>
                      <th className="text-right px-3 py-2 font-medium" style={{ color: theme.faint }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {crmMappings.map((row, i) => (
                      <tr key={i} className="border-t" style={{ borderColor: theme.ruleSubtle }}>
                        <td className="px-3 py-2 font-mono" style={{ color: theme.ink }}>{row.variable}</td>
                        <td className="px-3 py-2 font-mono" style={{ color: theme.ink }}>{row.field}</td>
                        <td className="px-3 py-2 text-right">
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
        </div>

        {/* Right column */}
        <div className="flex flex-col gap-4">
          {/* Knowledge & retrieval */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Knowledge & retrieval</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>RAG context retrieved per utterance from pgvector</div>
            </div>
            <div className="p-4 space-y-4">
              <ToggleRow title="Retrieval-augmented generation" sub="Ground responses in product knowledge and objection scripts — no hallucinated product details." checked={ragEnabled} onChange={setRagEnabled} theme={theme} />
              {ragEnabled && (
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Knowledge base index</label>
                  <select className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={ragIndex} onChange={e => setRagIndex(e.target.value)}>
                    <option>kb_hdfc_regalia_v3</option>
                    <option>kb_hdfc_millennia_v1</option>
                    <option>kb_collections_core</option>
                  </select>
                  <p className="text-[11px] mt-1" style={{ color: theme.faint }}>412 chunks · embeddings refreshed 9 Jun 2026</p>
                </div>
              )}
              <ToggleRow title="Regulatory disclosure block" sub="Non-skippable disclosure script read before any binding step." checked={disclosureEnabled} onChange={setDisclosureEnabled} theme={theme} />
              <ToggleRow title="Ambient call-floor audio" sub="Low-level background chatter mixed under the bot's voice — plays only while the bot speaks, never in silence." checked={ambientProfile !== 'silent'} onChange={(v) => setAmbientProfile(v ? 'callfloor' : 'silent')} theme={theme} />
            </div>
          </div>

          {/* Escalation rules */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b flex items-center justify-between" style={{ borderColor: theme.ruleSubtle }}>
              <div>
                <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Escalation rules</div>
                <div className="text-[12px]" style={{ color: theme.faint }}>Hand off to human agent queue with full context</div>
              </div>
              <button className="text-[12.5px] font-medium" style={{ color: theme.primary }}>+ Add rule</button>
            </div>
            <div className="p-4 space-y-2.5">
              {escalationRules.map((rule, i) => (
                <div key={i} className="flex items-center justify-between py-2.5 px-3 rounded-md border" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset }}>
                  <div className="flex items-center gap-2 text-[12.5px]">
                    <code className="px-1.5 py-0.5 rounded text-[11px] font-mono font-medium" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>{rule.condition}</code>
                    <span style={{ color: theme.faint }}>→</span>
                    <span style={{ color: theme.ink }}>{rule.target}</span>
                  </div>
                  <span className="px-2 py-0.5 rounded text-[10.5px] font-medium" style={{ backgroundColor: rule.badge === 'Compliance' ? theme.destructiveLight : theme.primaryLight, color: rule.badgeColor }}>{rule.badge}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Success criteria */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Success criteria</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>What this bot is measured on</div>
            </div>
            <div className="p-4">
              <KV k="Primary outcome" v="Warm lead escalated" theme={theme} />
              <KV k="Demo benchmark" v=">12 leads / 100 calls" theme={theme} />
              <KV k="Hard rejection handling" v="Log + suppress 30 days" theme={theme} />
            </div>
          </div>
        </div>
      </div>

      {/* Conversation flow — real editor (canvas + form), full width */}
      <div className="mt-4 rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
        <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
          <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Conversation flow</div>
          <div className="text-[12px]" style={{ color: theme.faint }}>Scripted state machine that scaffolds the LLM — nodes carry per-turn guidance, branches route on detected intent.</div>
        </div>
        <div className="p-4">
          <FlowEditor value={flow} onChange={setFlow} theme={theme} />
        </div>
      </div>

      {/* Save bar */}
      <div className="mt-4 flex items-center justify-between py-3 px-4 rounded-lg border" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
        <span className="text-[12px] flex items-center gap-1.5" style={{ color: theme.faint }}><Lock size={12} /> Changes apply on next session start. Active calls are not interrupted.</span>
        <div className="flex gap-3">
          <button onClick={onCancel} className="px-4 py-2 rounded-md border text-[13px] font-medium" style={{ borderColor: theme.rule, color: theme.inkMuted }}>Discard</button>
          <button onClick={handleSave} className="px-5 py-2 rounded-md text-[13px] font-bold text-white" style={{ backgroundColor: theme.primary }}>Save configuration</button>
        </div>
      </div>
    </div>
  );
}

function ToggleRow({ title, sub, checked, onChange, theme }: { title: string; sub: string; checked: boolean; onChange: (v: boolean) => void; theme: Theme }) {
  return (
    <div className="flex items-center justify-between py-2">
      <div className="pr-4">
        <div className="text-[13px] font-medium" style={{ color: theme.ink }}>{title}</div>
        <div className="text-[11.5px]" style={{ color: theme.faint }}>{sub}</div>
      </div>
      <button onClick={() => onChange(!checked)} className="w-9 h-5 rounded-full relative transition-colors shrink-0" style={{ backgroundColor: checked ? theme.primary : theme.rule }}>
        <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all" style={{ left: checked ? '18px' : '2px' }} />
      </button>
    </div>
  );
}
