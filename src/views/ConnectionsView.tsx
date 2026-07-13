import { useCallback, useEffect, useState } from 'react';
import { Plug, Plus } from 'lucide-react';

import {
  listConnections,
  createConnection,
  updateConnection,
  deactivateConnection,
  testConnection,
  KoelApiError,
  type ConnectionInput,
  type ConnectionTestResult,
} from '../lib/koel';
import type { ModelConnection, ModelConnectionKind } from '../types';
import type { Theme } from '../lib/theme';
import { ConnectionForm } from '../components/connections/ConnectionForm';
import { ConnectionCard } from '../components/connections/ConnectionCard';

const KIND_SECTIONS: { kind: ModelConnectionKind; title: string; sub: string }[] = [
  { kind: 'llm', title: 'Language models', sub: 'Reasoning + response generation (LLM)' },
  { kind: 'tts', title: 'Text-to-speech', sub: 'Voice synthesis for the bot (TTS)' },
  { kind: 'stt', title: 'Speech-to-text', sub: 'Customer transcription (STT)' },
];

type TestState = { loading: boolean; result?: ConnectionTestResult };

type Props = { theme: Theme; token: string | null; onToast: (m: string) => void };

// Model Connections manager — the org's BYOK provider credentials + endpoints,
// grouped by kind, with create/edit, a per-connection Test, and deactivate.
export function ConnectionsView({ theme, token, onToast }: Props) {
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<ModelConnection | null>(null);
  const [saving, setSaving] = useState(false);
  const [tests, setTests] = useState<Record<string, TestState>>({});

  const refresh = useCallback(async () => {
    if (!token) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      setConnections(await listConnections(token));
      setError(null);
    } catch (e) {
      setError(e instanceof KoelApiError ? e.message : 'Could not load model connections.');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openCreate = () => {
    setEditing(null);
    setShowForm(true);
  };

  const openEdit = (connection: ModelConnection) => {
    setEditing(connection);
    setShowForm(true);
  };

  const closeForm = () => {
    setShowForm(false);
    setEditing(null);
  };

  const handleSubmit = async (input: ConnectionInput) => {
    if (!token) return;
    setSaving(true);
    try {
      if (editing) {
        await updateConnection(token, editing.id, input);
        onToast(`Connection “${input.name}” updated.`);
      } else {
        await createConnection(token, input);
        onToast(`Connection “${input.name}” created.`);
      }
      closeForm();
      await refresh();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to save connection.');
    } finally {
      setSaving(false);
    }
  };

  const handleDeactivate = async (connection: ModelConnection) => {
    if (!token) return;
    try {
      await deactivateConnection(token, connection.id);
      onToast(`Connection “${connection.name}” deactivated.`);
      await refresh();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to deactivate connection.');
    }
  };

  const handleTest = async (connection: ModelConnection) => {
    if (!token) return;
    setTests((prev) => ({ ...prev, [connection.id]: { loading: true, result: prev[connection.id]?.result } }));
    try {
      const result = await testConnection(token, connection.id);
      setTests((prev) => ({ ...prev, [connection.id]: { loading: false, result } }));
    } catch (e) {
      const detail = e instanceof KoelApiError ? e.message : 'Test request failed.';
      setTests((prev) => ({ ...prev, [connection.id]: { loading: false, result: { ok: false, detail } } }));
    }
  };

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>Model connections</h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>
            {connections.length} connection{connections.length === 1 ? '' : 's'} · bring-your-own model providers
          </p>
        </div>
        {token && (
          <button onClick={showForm ? closeForm : openCreate} className="flex items-center gap-2 text-[13px] font-bold px-4 py-2 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
            <Plus size={14} /> {showForm ? 'Close' : 'New connection'}
          </button>
        )}
      </div>

      {!token && (
        <div className="rounded-lg border p-8 text-center text-[13px]" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.faint }}>
          <Plug size={28} className="mx-auto mb-3 opacity-40" style={{ color: theme.primary }} />
          Connect to a live VB_Svc backend to manage model connections.
        </div>
      )}

      {token && showForm && (
        <ConnectionForm
          key={editing?.id ?? 'new'}
          editing={editing}
          saving={saving}
          theme={theme}
          onSubmit={handleSubmit}
          onCancel={closeForm}
        />
      )}

      {token && (
        <>
          {loading && <div className="p-8 text-center text-[13px]" style={{ color: theme.faint }}>Loading connections…</div>}
          {!loading && error && <div className="p-8 text-center text-[13px]" style={{ color: theme.warning }}>{error}</div>}
          {!loading && !error && (
            <div className="flex flex-col gap-5">
              {KIND_SECTIONS.map(({ kind, title, sub }) => {
                const items = connections.filter((c) => c.kind === kind);
                return (
                  <section key={kind} className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
                    <div className="p-4 border-b flex items-center justify-between" style={{ borderColor: theme.ruleSubtle }}>
                      <div>
                        <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>{title}</div>
                        <div className="text-[12px]" style={{ color: theme.faint }}>{sub}</div>
                      </div>
                      <span className="text-[12px] font-medium px-2 py-0.5 rounded" style={{ backgroundColor: theme.ruleSubtle, color: theme.inkMuted }}>{items.length}</span>
                    </div>
                    <div className="p-4">
                      {items.length === 0 ? (
                        <p className="text-[12.5px] py-2" style={{ color: theme.faint }}>No {kind.toUpperCase()} connections yet.</p>
                      ) : (
                        <div className="flex flex-col gap-2.5">
                          {items.map((c) => (
                            <ConnectionCard
                              key={c.id}
                              connection={c}
                              testing={tests[c.id]?.loading ?? false}
                              testResult={tests[c.id]?.result}
                              theme={theme}
                              onEdit={() => openEdit(c)}
                              onTest={() => void handleTest(c)}
                              onDeactivate={() => void handleDeactivate(c)}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  </section>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}
