import type { Theme } from '../lib/theme';

// A labelled key/value row. Shared by the Config and Bot Builder review panels.
export function KV({ k, v, theme, mono }: { k: string; v: string; theme: Theme; mono?: boolean }) {
  return (
    <div className="flex justify-between text-[12.5px] py-1">
      <span style={{ color: theme.faint }}>{k}</span>
      <span className={mono ? 'font-mono' : ''} style={{ color: theme.ink, fontWeight: 500 }}>{v}</span>
    </div>
  );
}
