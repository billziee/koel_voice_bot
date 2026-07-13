import { useState } from 'react';

import type { Theme } from '../../lib/theme';
import type { ModelConnection, ModelConnectionKind } from '../../types';
import type { ConnectionInput } from '../../lib/koel';

// Create/edit form for a single model connection. Mount with a fresh `key` per
// target (e.g. the connection id or "new") so the seed state re-initialises when
// switching between create and edit. The api_key field is write-only: it is
// never pre-filled from the server and a blank value means "leave the stored key
// unchanged" on edit.
const KIND_OPTIONS: { value: ModelConnectionKind; label: string }[] = [
  { value: 'llm', label: 'LLM' },
  { value: 'tts', label: 'TTS' },
  { value: 'stt', label: 'STT' },
];

type Props = {
  editing: ModelConnection | null;
  saving: boolean;
  theme: Theme;
  onSubmit: (input: ConnectionInput) => void;
  onCancel: () => void;
};

const inputStyle = (theme: Theme) => ({
  borderColor: theme.rule,
  backgroundColor: theme.inset,
  color: theme.ink,
  fontFamily: '"DM Sans", sans-serif',
});

export function ConnectionForm({ editing, saving, theme, onSubmit, onCancel }: Props) {
  const [name, setName] = useState(editing?.name ?? '');
  const [kind, setKind] = useState<ModelConnectionKind>(editing?.kind ?? 'llm');
  const [provider, setProvider] = useState(editing?.provider ?? '');
  const [baseUrl, setBaseUrl] = useState(editing?.baseUrl ?? '');
  const [model, setModel] = useState(editing?.model ?? '');
  const [apiKey, setApiKey] = useState(''); // write-only — never seeded from the server

  const numParam = (k: string) =>
    editing && typeof editing.params[k] === 'number' ? String(editing.params[k]) : '';
  const strParam = (k: string) =>
    editing && typeof editing.params[k] === 'string' ? String(editing.params[k]) : '';

  const [temperature, setTemperature] = useState(numParam('temperature'));
  const [maxTokens, setMaxTokens] = useState(numParam('max_tokens'));
  const [voice, setVoice] = useState(strParam('voice'));
  const [language, setLanguage] = useState(strParam('language'));

  // Build the kind-specific params blob, omitting blank fields so we never send
  // NaN or empty strings the backend would have to defend against.
  const buildParams = (): Record<string, unknown> => {
    if (kind === 'llm') {
      const p: Record<string, unknown> = {};
      if (temperature.trim() !== '') p.temperature = Number(temperature);
      if (maxTokens.trim() !== '') p.max_tokens = Number(maxTokens);
      return p;
    }
    if (kind === 'tts') return voice.trim() ? { voice: voice.trim() } : {};
    return language.trim() ? { language: language.trim() } : {};
  };

  const canSubmit = name.trim().length > 0 && provider.trim().length > 0 && !saving;

  const handleSubmit = () => {
    if (!canSubmit) return;
    onSubmit({
      name: name.trim(),
      kind,
      provider: provider.trim(),
      baseUrl: baseUrl.trim() || undefined,
      model: model.trim() || undefined,
      params: buildParams(),
      apiKey: apiKey || undefined, // blank → omitted → key left unchanged
    });
  };

  const labelClass = 'block text-[12px] font-medium mb-1.5';
  const fieldClass = 'w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2';

  return (
    <div className="rounded-lg border p-4 mb-6 grid gap-4" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <div className="flex items-center justify-between">
        <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>
          {editing ? 'Edit connection' : 'New model connection'}
        </div>
        <span className="text-[11.5px]" style={{ color: theme.faint }}>
          {editing ? editing.provider : 'BYOK · org-scoped credential'}
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className={labelClass} style={{ color: theme.ink2 }}>Name</label>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Groq Llama 3.1" className={fieldClass} style={inputStyle(theme)} />
        </div>
        <div>
          <label className={labelClass} style={{ color: theme.ink2 }}>Kind</label>
          <select value={kind} onChange={(e) => setKind(e.target.value as ModelConnectionKind)} className={fieldClass} style={inputStyle(theme)}>
            {KIND_OPTIONS.map((k) => (
              <option key={k.value} value={k.value}>{k.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className={labelClass} style={{ color: theme.ink2 }}>Provider</label>
          <input value={provider} onChange={(e) => setProvider(e.target.value)} placeholder="e.g. groq, nvidia, openai" className={fieldClass} style={inputStyle(theme)} />
        </div>
        <div>
          <label className={labelClass} style={{ color: theme.ink2 }}>Model</label>
          <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="e.g. llama-3.1-70b" className={`${fieldClass} font-mono`} style={inputStyle(theme)} />
        </div>
        <div className="sm:col-span-2">
          <label className={labelClass} style={{ color: theme.ink2 }}>Base URL</label>
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.provider.com/v1 (optional)" className={`${fieldClass} font-mono`} style={inputStyle(theme)} />
        </div>
        <div className="sm:col-span-2">
          <label className={labelClass} style={{ color: theme.ink2 }}>API key</label>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            autoComplete="new-password"
            placeholder={editing?.apiKeySet ? 'Key set ✓ — leave blank to keep' : 'Paste the provider API key'}
            className={fieldClass}
            style={inputStyle(theme)}
          />
          <p className="text-[11px] mt-1" style={{ color: theme.faint }}>
            Write-only — stored server-side and never shown again.{editing ? ' Leave blank to keep the current key.' : ''}
          </p>
        </div>
      </div>

      {/* Kind-specific params */}
      {kind === 'llm' && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass} style={{ color: theme.ink2 }}>Temperature</label>
            <input type="number" step={0.1} min={0} max={2} value={temperature} onChange={(e) => setTemperature(e.target.value)} placeholder="0.7" className={fieldClass} style={inputStyle(theme)} />
          </div>
          <div>
            <label className={labelClass} style={{ color: theme.ink2 }}>Max tokens</label>
            <input type="number" step={1} min={1} value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} placeholder="1024" className={fieldClass} style={inputStyle(theme)} />
          </div>
        </div>
      )}
      {kind === 'tts' && (
        <div>
          <label className={labelClass} style={{ color: theme.ink2 }}>Voice</label>
          <input value={voice} onChange={(e) => setVoice(e.target.value)} placeholder="e.g. Kore, meera" className={fieldClass} style={inputStyle(theme)} />
        </div>
      )}
      {kind === 'stt' && (
        <div>
          <label className={labelClass} style={{ color: theme.ink2 }}>Language</label>
          <input value={language} onChange={(e) => setLanguage(e.target.value)} placeholder="e.g. hi, en, hinglish" className={fieldClass} style={inputStyle(theme)} />
        </div>
      )}

      <div className="flex justify-end gap-3">
        <button onClick={onCancel} className="px-4 py-2 rounded-md border text-[13px] font-medium" style={{ borderColor: theme.rule, color: theme.inkMuted }}>
          Cancel
        </button>
        <button
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="px-5 py-2 rounded-md text-[13px] font-bold text-white disabled:opacity-50"
          style={{ backgroundColor: theme.primary }}
        >
          {saving ? 'Saving…' : editing ? 'Save changes' : 'Create connection'}
        </button>
      </div>
    </div>
  );
}
