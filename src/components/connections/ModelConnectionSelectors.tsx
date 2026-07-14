import type { Theme } from '../../lib/theme';
import type { ConnectionSelection, ModelConnection, ModelConnectionKind } from '../../types';

// Three dropdowns — LLM / TTS / STT — each listing the org's connections of that
// kind plus a "Default (built-in)" option. Selecting the default clears the id
// (value = undefined), so the bot falls back to the platform's built-in model.
// Purely presentational: the parent owns the fetched list and the selection.
const KINDS: { kind: ModelConnectionKind; label: string }[] = [
  { kind: 'llm', label: 'LLM model' },
  { kind: 'tts', label: 'TTS voice' },
  { kind: 'stt', label: 'STT model' },
];

const DEFAULT_OPTION = '__default__';

type Props = {
  connections: ModelConnection[];
  value: ConnectionSelection;
  onChange: (next: ConnectionSelection) => void;
  theme: Theme;
};

export function ModelConnectionSelectors({ connections, value, onChange, theme }: Props) {
  const handleSelect = (kind: ModelConnectionKind, raw: string) => {
    // Immutable update: replace just this kind, dropping it entirely when Default.
    const next: ConnectionSelection = { ...value };
    if (raw === DEFAULT_OPTION) delete next[kind];
    else next[kind] = raw;
    onChange(next);
  };

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
      {KINDS.map(({ kind, label }) => {
        const current = value[kind];
        // Show active connections of this kind, plus the currently-selected one
        // even if it was since deactivated (so the selection stays visible).
        const options = connections.filter(
          (c) => c.kind === kind && (c.active || c.id === current),
        );
        const currentKnown = !current || options.some((c) => c.id === current);
        return (
          <div key={kind}>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>
              {label}
            </label>
            <select
              value={current ?? DEFAULT_OPTION}
              onChange={(e) => handleSelect(kind, e.target.value)}
              className="w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2"
              style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}
            >
              <option value={DEFAULT_OPTION}>Default (built-in)</option>
              {options.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.model ? ` · ${c.model}` : ''}
                  {c.active ? '' : ' (inactive)'}
                </option>
              ))}
              {/* Selected id not in the fetched list (e.g. list failed to load) —
                  keep a placeholder so the controlled select stays consistent. */}
              {current && !currentKnown && <option value={current}>{current} (unavailable)</option>}
            </select>
          </div>
        );
      })}
    </div>
  );
}
