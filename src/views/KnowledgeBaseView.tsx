import { useState, useRef, useCallback, useEffect } from 'react';
import { BookOpen, Plus, Search } from 'lucide-react';

import {
  getKnowledgeIndexes,
  createKnowledgeChunk,
  uploadKnowledgeFile,
  KoelApiError,
  type KnowledgeIndex,
} from '../lib/koel';
import type { Theme } from '../lib/theme';

export function KnowledgeBaseView({ theme, token, onToast }: { theme: Theme; token: string | null; onToast: (m: string) => void }) {
  const [search, setSearch] = useState('');
  const [indexes, setIndexes] = useState<KnowledgeIndex[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ rag_index: '', use_case: 'credit_card', title: '', content: '' });
  const [saving, setSaving] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [uploadMeta, setUploadMeta] = useState({ rag_index: '', use_case: 'credit_card' });
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    if (!token) { setLoading(false); return; }
    setLoading(true);
    try {
      setIndexes(await getKnowledgeIndexes(token));
      setError(null);
    } catch (e) {
      setError(e instanceof KoelApiError ? e.message : 'Could not load knowledge indexes.');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void refresh(); }, [refresh]);

  const submit = async () => {
    if (!token || !form.rag_index.trim() || !form.title.trim() || !form.content.trim()) return;
    setSaving(true);
    try {
      await createKnowledgeChunk(token, { ...form, chunk: form.content.length > 1200 });
      onToast(`Knowledge added to “${form.rag_index}” and embedded for retrieval.`);
      setForm({ rag_index: '', use_case: 'credit_card', title: '', content: '' });
      setShowForm(false);
      await refresh();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to add knowledge.');
    } finally {
      setSaving(false);
    }
  };

  const doUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!token || !uploadMeta.rag_index.trim() || !file) return;
    setUploading(true);
    try {
      const chunks = await uploadKnowledgeFile(token, {
        file, ragIndex: uploadMeta.rag_index.trim(), useCase: uploadMeta.use_case,
      });
      onToast(`Ingested “${file.name}” → ${chunks.length} chunk${chunks.length === 1 ? '' : 's'} embedded into “${uploadMeta.rag_index}”.`);
      if (fileRef.current) fileRef.current.value = '';
      setUploadMeta({ rag_index: '', use_case: 'credit_card' });
      setShowUpload(false);
      await refresh();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'File upload failed.');
    } finally {
      setUploading(false);
    }
  };

  const filtered = indexes.filter(kb => kb.rag_index.toLowerCase().includes(search.toLowerCase()));
  const totalChunks = indexes.reduce((s, k) => s + k.chunk_count, 0);

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>Knowledge base</h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>{indexes.length} indexes · {totalChunks} chunks</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => { setShowUpload(v => !v); setShowForm(false); }} className="flex items-center gap-2 text-[13px] font-semibold px-4 py-2 rounded-md border" style={{ borderColor: theme.rule, color: theme.ink }}>
            <BookOpen size={14} /> {showUpload ? 'Close' : 'Upload file'}
          </button>
          <button onClick={() => { setShowForm(v => !v); setShowUpload(false); }} className="flex items-center gap-2 text-[13px] font-bold px-4 py-2 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
            <Plus size={14} /> {showForm ? 'Close' : 'New chunk'}
          </button>
        </div>
      </div>

      {showUpload && (
        <div className="rounded-lg border p-4 mb-4 grid gap-3" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="grid grid-cols-2 gap-3">
            <input value={uploadMeta.rag_index} onChange={e => setUploadMeta({ ...uploadMeta, rag_index: e.target.value })} placeholder="RAG index (e.g. cc_hdfc)" className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
            <select value={uploadMeta.use_case} onChange={e => setUploadMeta({ ...uploadMeta, use_case: e.target.value })} className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }}>
              {['credit_card', 'collections', 'personal_loan', 'insurance', 'custom'].map(u => <option key={u} value={u}>{u}</option>)}
            </select>
          </div>
          <input ref={fileRef} type="file" accept=".txt,.md,.csv,.pdf,text/plain,application/pdf" className="text-[13px]" style={{ color: theme.ink }} />
          <p className="text-[11px]" style={{ color: theme.faint }}>Text or PDF, up to 2 MB. The file is split into passages, embedded, and stored under the index above.</p>
          <div className="flex justify-end">
            <button onClick={() => void doUpload()} disabled={uploading} className="text-[13px] font-bold px-4 py-2 rounded-md text-white disabled:opacity-50" style={{ backgroundColor: theme.primary }}>
              {uploading ? 'Ingesting…' : 'Upload & embed'}
            </button>
          </div>
        </div>
      )}

      {showForm && (
        <div className="rounded-lg border p-4 mb-4 grid gap-3" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="grid grid-cols-2 gap-3">
            <input value={form.rag_index} onChange={e => setForm({ ...form, rag_index: e.target.value })} placeholder="RAG index (e.g. cc_hdfc)" className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
            <select value={form.use_case} onChange={e => setForm({ ...form, use_case: e.target.value })} className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }}>
              {['credit_card', 'collections', 'personal_loan', 'insurance', 'custom'].map(u => <option key={u} value={u}>{u}</option>)}
            </select>
          </div>
          <input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="Title (e.g. Objection: annual fee)" className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
          <textarea value={form.content} onChange={e => setForm({ ...form, content: e.target.value })} placeholder="Knowledge content — this is embedded and used to ground responses." rows={3} className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2 resize-y" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
          <div className="flex justify-end">
            <button onClick={() => void submit()} disabled={saving} className="text-[13px] font-bold px-4 py-2 rounded-md text-white disabled:opacity-50" style={{ backgroundColor: theme.primary }}>
              {saving ? 'Embedding…' : 'Add & embed'}
            </button>
          </div>
        </div>
      )}

      <div className="mb-4">
        <div className="relative">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: theme.faint }} />
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search indexes…" className="w-full border rounded-md pl-9 pr-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} />
        </div>
      </div>

      <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
        <table className="w-full text-[13px]">
          <thead>
            <tr style={{ backgroundColor: theme.inset }}>
              <th className="text-left px-4 py-2.5 font-medium text-[12px]" style={{ color: theme.faint }}>Index ID</th>
              <th className="text-right px-4 py-2.5 font-medium text-[12px]" style={{ color: theme.faint }}>Chunks</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(kb => (
              <tr key={kb.rag_index} className="border-t" style={{ borderColor: theme.ruleSubtle }}>
                <td className="px-4 py-3 font-mono text-[12px]" style={{ color: theme.primary }}>{kb.rag_index}</td>
                <td className="px-4 py-3 text-right font-mono" style={{ color: theme.ink }}>{kb.chunk_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading && <div className="p-8 text-center text-[13px]" style={{ color: theme.faint }}>Loading indexes…</div>}
        {!loading && error && <div className="p-8 text-center text-[13px]" style={{ color: theme.warning }}>{error}</div>}
        {!loading && !error && filtered.length === 0 && (
          <div className="p-8 text-center text-[13px]" style={{ color: theme.faint }}>No indexes yet — add a knowledge chunk to create one.</div>
        )}
      </div>
    </div>
  );
}
