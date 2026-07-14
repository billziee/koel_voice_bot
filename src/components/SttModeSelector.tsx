import type { Theme } from '../lib/theme';
import type { SttMode } from '../types';

// Two SEPARATE, always-visible STT modes. Chunk is the default and most robust;
// WS is a deliberate operator opt-in. Both options stay on screen (radio group),
// never collapsed into a dropdown — the choice is a considered operator decision.
const STT_MODE_OPTIONS: { value: SttMode; label: string; description: string }[] = [
  {
    value: 'chunk',
    label: 'Chunk (batch)',
    description: 'Transcribe each full utterance in one request. Default; most robust.',
  },
  {
    value: 'ws',
    label: 'WebSocket streaming',
    description: 'Persistent socket to Sarvam Saaras; lower per-turn overhead. Falls back to chunk on any error.',
  },
];

interface SttModeSelectorProps {
  value: SttMode;
  onChange: (mode: SttMode) => void;
  theme: Theme;
}

// Reuses the bordered selectable-card pattern already used for language pills and
// conversation-flow branches (primary border + primaryLight fill when selected).
export function SttModeSelector({ value, onChange, theme }: SttModeSelectorProps) {
  return (
    <div>
      <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>STT mode</label>
      <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Speech-to-text mode">
        {STT_MODE_OPTIONS.map(opt => {
          const selected = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => onChange(opt.value)}
              className="text-left p-3 rounded-md border transition-colors"
              style={{
                borderColor: selected ? theme.primary : theme.rule,
                backgroundColor: selected ? theme.primaryLight : 'transparent',
              }}
            >
              <span className="flex items-center gap-1.5 text-[12.5px] font-medium" style={{ color: selected ? theme.primary : theme.ink }}>
                <span
                  className="w-3 h-3 rounded-full border flex items-center justify-center shrink-0"
                  style={{ borderColor: selected ? theme.primary : theme.rule }}
                >
                  {selected && <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: theme.primary }} />}
                </span>
                {opt.label}
              </span>
              <span className="block text-[11px] mt-1.5" style={{ color: theme.faint }}>{opt.description}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
