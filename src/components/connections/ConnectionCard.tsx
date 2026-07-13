import { AlertTriangle, Ban, CheckCircle2, Pencil, Play } from 'lucide-react';

import type { Theme } from '../../lib/theme';
import type { ModelConnection } from '../../types';
import type { ConnectionTestResult } from '../../lib/koel';

type Props = {
  connection: ModelConnection;
  testing: boolean;
  testResult?: ConnectionTestResult;
  theme: Theme;
  onEdit: () => void;
  onTest: () => void;
  onDeactivate: () => void;
};

// One connection, rendered as a bordered card matching the config/flow surfaces.
// Shows provider/model, whether a key is set, the active state, a params summary,
// and the last /test outcome inline.
export function ConnectionCard({ connection, testing, testResult, theme, onEdit, onTest, onDeactivate }: Props) {
  const paramsSummary = Object.entries(connection.params)
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join(' · ');

  return (
    <div className="rounded-md border p-3.5" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset, opacity: connection.active ? 1 : 0.72 }}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13.5px] font-semibold truncate" style={{ color: theme.ink }}>{connection.name}</span>
            <span className="px-1.5 py-0.5 rounded text-[10.5px] font-medium" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>
              {connection.provider}
            </span>
            {!connection.active && (
              <span className="px-1.5 py-0.5 rounded text-[10.5px] font-medium" style={{ backgroundColor: theme.ruleSubtle, color: theme.faint }}>
                Inactive
              </span>
            )}
          </div>
          <div className="text-[11.5px] mt-1 font-mono truncate" style={{ color: theme.inkMuted }}>
            {connection.model || 'default model'}{connection.baseUrl ? ` @ ${connection.baseUrl}` : ''}
          </div>
          <div className="flex items-center gap-3 mt-1.5 flex-wrap">
            <span className="text-[11px] flex items-center gap-1" style={{ color: connection.apiKeySet ? theme.green : theme.warning }}>
              {connection.apiKeySet ? <CheckCircle2 size={11} /> : <AlertTriangle size={11} />}
              {connection.apiKeySet ? 'Key set ✓' : 'No key'}
            </span>
            {paramsSummary && <span className="text-[11px] font-mono" style={{ color: theme.faint }}>{paramsSummary}</span>}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            onClick={onTest}
            disabled={testing}
            title="Test connection"
            className="flex items-center gap-1 text-[12px] font-medium px-2.5 py-1.5 rounded-md border disabled:opacity-50"
            style={{ borderColor: theme.rule, color: theme.ink }}
          >
            <Play size={12} /> {testing ? 'Testing…' : 'Test'}
          </button>
          <button onClick={onEdit} title="Edit connection" className="p-1.5 rounded-md border" style={{ borderColor: theme.rule, color: theme.inkMuted }}>
            <Pencil size={13} />
          </button>
          {connection.active && (
            <button onClick={onDeactivate} title="Deactivate connection" className="p-1.5 rounded-md border" style={{ borderColor: theme.rule, color: theme.destructive }}>
              <Ban size={13} />
            </button>
          )}
        </div>
      </div>

      {testResult && (
        <div
          className="mt-2.5 flex items-start gap-1.5 text-[12px] px-2.5 py-2 rounded-md"
          style={{ backgroundColor: testResult.ok ? theme.greenLight : theme.destructiveLight, color: testResult.ok ? theme.green : theme.destructive }}
        >
          {testResult.ok ? <CheckCircle2 size={13} className="mt-0.5 shrink-0" /> : <AlertTriangle size={13} className="mt-0.5 shrink-0" />}
          <span>
            {testResult.ok ? 'Reachable' : 'Failed'}
            {typeof testResult.latencyMs === 'number' ? ` · ${testResult.latencyMs} ms` : ''}
            {testResult.detail ? ` — ${testResult.detail}` : ''}
          </span>
        </div>
      )}
    </div>
  );
}
