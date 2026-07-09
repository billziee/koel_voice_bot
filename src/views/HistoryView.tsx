import { useState, useEffect, useRef } from 'react';
import { Activity, ChevronDown, ChevronRight, Clock, Download, Filter, Languages, Pause, Play, Search, User, X } from 'lucide-react';

import type { CallHistory } from '../types';
import type { Theme } from '../lib/theme';
import { formatTime } from '../lib/format';
import { TranscriptMessage } from '../components/TranscriptMessage';

export function HistoryView({ calls, onExported, theme }: { calls: CallHistory[]; onExported: () => void; theme: Theme; darkMode: boolean }) {
  const [selectedCallId, setSelectedCallId] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<string>('date_desc');
  const [botFilter, setBotFilter] = useState<string>('all');
  const [outcomeFilter, setOutcomeFilter] = useState<string>('all');
  const [languageFilter, setLanguageFilter] = useState<string>('all');
  const [search, setSearch] = useState('');
  // Item 3: date range filter
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [showEvents, setShowEvents] = useState(false); // Item 35: toggle OTel trace in detail
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

  const botNames = Array.from(new Set(calls.map(c => c.bot)));
  const languages = Array.from(new Set(calls.map(c => c.language)));
  // Item 37: filter by specific outcome values instead of status buckets
  const outcomeValues = Array.from(new Set(calls.map(c => c.outcome)));

  const filtered = calls.filter(c => {
    if (botFilter !== 'all' && c.bot !== botFilter) return false;
    if (languageFilter !== 'all' && c.language !== languageFilter) return false;
    if (outcomeFilter !== 'all' && c.outcome !== outcomeFilter) return false;
    // Item 3: date filter
    if (dateFrom && c.timestamp < new Date(dateFrom).getTime()) return false;
    if (dateTo && c.timestamp > new Date(dateTo + 'T23:59:59').getTime()) return false;
    if (search && !`${c.id} ${c.customer} ${c.outcome} ${c.customerId || ''}`.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  // Item 44: clear selection when filtered set excludes it; reset page
  useEffect(() => {
    if (selectedCallId && !filtered.find(c => c.id === selectedCallId)) {
      setSelectedCallId(null);
    }
    setCurrentPage(1);
  }, [filtered.length, selectedCallId]);

  const sortedCalls = [...filtered].sort((a, b) => {
    if (sortBy === 'date_desc') return b.timestamp - a.timestamp;
    if (sortBy === 'date_asc') return a.timestamp - b.timestamp;
    if (sortBy === 'customer') return a.customer.localeCompare(b.customer);
    if (sortBy === 'duration') return b.durationSec - a.durationSec;
    return 0;
  });

  // Item 44: look up from filtered, not all calls
  const selectedCall = filtered.find(c => c.id === selectedCallId);

  // Item 44: dynamic colSpan
  const tableColCount = selectedCallId ? 4 : 8;

  const totalPages = Math.max(1, Math.ceil(sortedCalls.length / pageSize));
  const paginatedCalls = sortedCalls.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const exportCsv = () => {
    // Item 38: ISO timestamp; Item 16: Customer ID; Item 47: UTF-8 BOM
    const header = ['Session ID', 'Bot', 'Customer', 'Customer ID', 'Phone', 'Language', 'Date', 'Timestamp (ISO)', 'Duration (s)', 'Outcome', 'Avg Latency (ms)'];
    const rows = sortedCalls.map(c => [c.id, c.bot, c.customer, c.customerId || '', c.phone, c.language, c.dateStr, new Date(c.timestamp).toISOString(), String(c.durationSec), c.outcome, String(c.avgLatencyMs)]);
    const csv = '\uFEFF' + [header, ...rows].map(r => r.map(v => `"${v.replace(/"/g, '""')}"`).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'koel_call_history.csv';
    a.click();
    URL.revokeObjectURL(url);
    onExported();
  };

  return (
    <div className="max-w-7xl mx-auto p-4 md:p-8 flex flex-col h-full animate-in fade-in duration-300">
      <div className="flex flex-wrap justify-between items-end mb-6 gap-4">
        <div>
          <h1 className="text-3xl font-semibold mb-2" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>Call History</h1>
          <p style={{ color: theme.inkMuted }}>Review transcripts, latencies, and outcomes for all sessions.</p>
        </div>
        <button onClick={exportCsv} className="flex items-center gap-2 px-4 py-2 rounded-lg text-white font-medium transition-opacity hover:opacity-90 shadow-sm text-sm focus-visible:ring-2" style={{ backgroundColor: theme.primary }}>
          <Download size={16} /> Export CSV
        </button>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3 mb-6">
        <div className="flex items-center gap-2 border rounded-lg px-3 py-1.5 shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <Search size={15} style={{ color: theme.inkMuted }} />
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search ID, customer, outcome…" className="bg-transparent text-sm font-medium outline-none w-40 md:w-52" style={{ color: theme.ink }} />
        </div>
        <FilterSelect value={botFilter} onChange={setBotFilter} options={[['all', 'All Bots'], ...botNames.map(n => [n, n] as [string, string])]} theme={theme} />
        {/* Item 37: filter by specific outcome value */}
        <FilterSelect value={outcomeFilter} onChange={setOutcomeFilter} options={[['all', 'All Outcomes'], ...outcomeValues.map(o => [o, o] as [string, string])]} theme={theme} />
        <FilterSelect value={languageFilter} onChange={setLanguageFilter} options={[['all', 'All Languages'], ...languages.map(l => [l, l] as [string, string])]} theme={theme} />
        <FilterSelect value={sortBy} onChange={setSortBy} options={[['date_desc', 'Date (Newest)'], ['date_asc', 'Date (Oldest)'], ['customer', 'Customer Name'], ['duration', 'Duration (Longest)']]} theme={theme} />
        {/* Item 3: date range filter */}
        <div className="flex items-center gap-2">
          <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs font-medium outline-none" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink }} aria-label="Filter from date" />
          <span className="text-xs" style={{ color: theme.inkMuted }}>to</span>
          <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs font-medium outline-none" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink }} aria-label="Filter to date" />
        </div>
        <span className="text-xs font-medium ml-auto" style={{ color: theme.inkMuted }}>{sortedCalls.length} of {calls.length} sessions</span>
      </div>

      <div className="flex gap-6 flex-1 min-h-[560px] flex-col md:flex-row">
        {/* Call table */}
        <div className={`rounded-lg border flex flex-col transition-all duration-300 shadow-sm ${selectedCallId ? 'md:w-1/2' : 'w-full'}`} style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="overflow-y-auto flex-1">
            <table className="w-full text-left border-collapse">
              <thead className="sticky top-0 z-10 shadow-sm" style={{ backgroundColor: theme.surface }}>
                <tr className="border-b text-[11px] uppercase tracking-wider font-bold" style={{ borderColor: theme.rule, color: theme.inkMuted }}>
                  <th className="p-4">Session ID</th>
                  <th className="p-4">Customer</th>
                  {!selectedCallId && <th className="p-4">Bot Instance</th>}
                  {!selectedCallId && <th className="p-4">Language</th>}
                  {!selectedCallId && <th className="p-4">Date</th>}
                  {!selectedCallId && <th className="p-4">Duration</th>}
                  <th className="p-4">Outcome</th>
                  <th className="p-4 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="text-sm">
                {paginatedCalls.length === 0 && (
                  <tr><td colSpan={tableColCount} className="p-10 text-center text-sm" style={{ color: theme.inkMuted }}>No sessions match the current filters.</td></tr>
                )}
                {paginatedCalls.map(call => (
                  <tr
                    key={call.id}
                    onClick={() => setSelectedCallId(call.id)}
                    onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && setSelectedCallId(call.id)}
                    tabIndex={0}
                    role="button"
                    className={`border-b cursor-pointer transition-colors group focus-visible:ring-2 focus-visible:ring-inset ${selectedCallId === call.id ? 'border-l-4' : 'border-l-4 border-l-transparent'}`}
                    style={{
                      borderColor: theme.rule,
                      borderLeftColor: selectedCallId === call.id ? theme.primary : 'transparent',
                      backgroundColor: selectedCallId === call.id ? theme.primarySubtle : 'transparent',
                    }}
                  >
                    <td className="p-4 font-mono font-medium text-xs" style={{ color: theme.ink }}>{call.id}</td>
                    {/* Item 16: show customer ID */}
                    <td className="p-4">
                      <span className="font-semibold" style={{ color: theme.ink }}>{call.customer}</span>
                      {call.customerId && <span className="block text-[10px] font-mono" style={{ color: theme.inkMuted }}>{call.customerId}</span>}
                    </td>
                    {!selectedCallId && <td className="p-4" style={{ color: theme.inkMuted }}>{call.bot}</td>}
                    {!selectedCallId && <td className="p-4 text-xs" style={{ color: theme.inkMuted }}>{call.language}</td>}
                    {!selectedCallId && <td className="p-4 text-xs" style={{ color: theme.inkMuted }}>{call.dateStr}</td>}
                    {!selectedCallId && <td className="p-4 font-mono text-xs" style={{ color: theme.inkMuted }}>{call.durationStr}</td>}
                    <td className="p-4">
                      <span className={`inline-flex px-2.5 py-1 rounded-md text-[11px] uppercase tracking-wide font-bold
                        ${call.status === 'green' ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400' : ''}
                        ${call.status === 'orange' ? 'bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400' : ''}
                        ${call.status === 'red' ? 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400' : ''}
                        ${call.status === 'gray' ? 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400' : ''}
                      `}>
                        {call.outcome}
                      </span>
                    </td>
                    <td className="p-4 text-right">
                      {/* Item 54: visible on focus */}
                      <button className={`text-sm font-bold flex items-center justify-end w-full gap-1 transition-opacity focus-visible:opacity-100 ${selectedCallId === call.id ? '' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'}`} style={{ color: theme.primary }}>
                        {selectedCallId === call.id ? 'Viewing' : 'View'} <ChevronRight size={14} className={selectedCallId === call.id ? 'hidden' : 'block'} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t text-[12.5px]" style={{ borderColor: theme.ruleSubtle, color: theme.inkMuted }}>
              <span>Showing {(currentPage - 1) * pageSize + 1}–{Math.min(currentPage * pageSize, sortedCalls.length)} of {sortedCalls.length}</span>
              <div className="flex gap-1">
                {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
                  const pg = totalPages <= 5 ? i + 1 : currentPage <= 3 ? i + 1 : currentPage >= totalPages - 2 ? totalPages - 4 + i : currentPage - 2 + i;
                  return (
                    <button key={pg} onClick={() => setCurrentPage(pg)} className="w-[26px] h-[26px] flex items-center justify-center rounded-md text-[12.5px]" style={{ backgroundColor: currentPage === pg ? theme.primaryLight : 'transparent', color: currentPage === pg ? theme.primary : theme.inkMuted, fontWeight: currentPage === pg ? 500 : 400 }}>
                      {pg}
                    </button>
                  );
                })}
                {totalPages > 5 && <span className="w-[26px] h-[26px] flex items-center justify-center">…</span>}
                {totalPages > 5 && (
                  <button onClick={() => setCurrentPage(totalPages)} className="w-[26px] h-[26px] flex items-center justify-center rounded-md text-[12.5px]" style={{ color: currentPage === totalPages ? theme.primary : theme.inkMuted, backgroundColor: currentPage === totalPages ? theme.primaryLight : 'transparent' }}>
                    {totalPages}
                  </button>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Right: transcript + replay */}
        {selectedCall && (
          <div className="md:w-1/2 rounded-lg border flex flex-col overflow-hidden shadow-md animate-in fade-in slide-in-from-right-4" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-5 border-b flex justify-between items-start" style={{ borderColor: theme.rule, backgroundColor: theme.sidebarBg }}>
              <div>
                <h3 className="font-bold text-lg mb-1" style={{ color: theme.ink }}>Session Transcript</h3>
                <div className="flex flex-wrap gap-3 text-xs font-medium" style={{ color: theme.inkMuted }}>
                  <span className="px-2 py-1 rounded border shadow-sm font-mono" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>{selectedCall.id}</span>
                  <span className="flex items-center gap-1"><User size={12} /> {selectedCall.customer} ({selectedCall.phone})</span>
                  {/* Item 16: Customer ID */}
                  {selectedCall.customerId && <span className="font-mono">{selectedCall.customerId}</span>}
                  <span className="flex items-center gap-1"><Clock size={12} /> {selectedCall.dateStr}</span>
                  <span className="flex items-center gap-1"><Languages size={12} /> {selectedCall.language}</span>
                </div>
              </div>
              <button onClick={() => setSelectedCallId(null)} className="p-1.5 rounded-md transition-colors border shadow-sm focus-visible:ring-2" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.inkMuted }} aria-label="Close transcript panel">
                <X size={16} />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-6 flex flex-col gap-5" style={{ backgroundColor: theme.transcriptBg }}>
              {selectedCall.transcript.map(msg => (
                <TranscriptMessage key={msg.id} msg={msg} compact theme={theme} />
              ))}
              {selectedCall.transcript.length === 0 && (
                <p className="text-sm italic text-center mt-10" style={{ color: theme.inkMuted }}>No utterances captured before the session ended.</p>
              )}
            </div>

            {/* Item 35: OTel trace in history detail */}
            {selectedCall.events && selectedCall.events.length > 0 && (
              <div className="border-t" style={{ borderColor: theme.rule }}>
                <button onClick={() => setShowEvents(!showEvents)} className="w-full p-3 flex items-center justify-between text-xs font-bold uppercase tracking-wider" style={{ color: theme.inkMuted, backgroundColor: theme.surface }}>
                  <span className="flex items-center gap-2"><Activity size={12} /> Session Events ({selectedCall.events.length})</span>
                  <ChevronDown size={14} className={`transition-transform ${showEvents ? 'rotate-180' : ''}`} />
                </button>
                {showEvents && (
                  <div className="p-3 font-mono text-[10px] leading-relaxed max-h-40 overflow-y-auto flex flex-col gap-1" style={{ backgroundColor: theme.otelBg }}>
                    {selectedCall.events.map(ev => (
                      <div key={ev.id} className="flex gap-2">
                        <span className="text-gray-500 shrink-0">[{ev.t}]</span>
                        <span className={ev.level === 'ok' ? 'text-green-400' : ev.level === 'warn' ? 'text-yellow-400' : 'text-blue-300'}>{ev.label}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <AudioReplayBar call={selectedCall} theme={theme} />
          </div>
        )}
      </div>
    </div>
  );
}

function FilterSelect({ value, onChange, options, theme }: { value: string; onChange: (v: string) => void; options: [string, string][]; theme: Theme }) {
  return (
    <div className="flex items-center gap-2 border rounded-lg px-3 py-1.5 shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <Filter size={14} style={{ color: theme.inkMuted }} />
      <select className="bg-transparent text-sm font-medium outline-none cursor-pointer" style={{ color: theme.ink }} value={value} onChange={e => onChange(e.target.value)}>
        {options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      </select>
    </div>
  );
}

// Item 26: fix replay pacing; Item 39: data residency; Item 49: pure updaters
function AudioReplayBar({ call, theme }: { call: CallHistory; theme: Theme }) {
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const intervalRef = useRef<number | null>(null);

  useEffect(() => {
    setPlaying(false);
    setProgress(0);
    if (intervalRef.current) window.clearInterval(intervalRef.current);
  }, [call.id]);

  useEffect(() => {
    if (playing) {
      intervalRef.current = window.setInterval(() => {
        setProgress(prev => {
          // Item 26: fix pacing — exact step, clamp to 100
          const step = 100 / Math.max(call.durationSec, 1);
          const next = Math.min(100, prev + step);
          return next;
        });
      }, 1000);
    } else if (intervalRef.current) {
      window.clearInterval(intervalRef.current);
    }
    return () => { if (intervalRef.current) window.clearInterval(intervalRef.current); };
  }, [playing, call.durationSec]);

  // Item 49: stop playing when progress hits 100 — outside updater
  useEffect(() => {
    if (progress >= 100 && playing) setPlaying(false);
  }, [progress, playing]);

  return (
    <div className="p-4 border-t" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <div className="flex items-center gap-3 mb-3">
        <button
          onClick={() => { if (progress >= 100) setProgress(0); setPlaying(p => !p); }}
          className="w-9 h-9 rounded-full flex items-center justify-center text-white shadow-md hover:scale-105 transition-transform shrink-0 focus-visible:ring-2"
          style={{ backgroundColor: theme.primary }}
          aria-label={playing ? 'Pause replay' : 'Play replay'}
        >
          {playing ? <Pause size={15} /> : <Play size={15} fill="currentColor" className="ml-0.5" />}
        </button>
        <div className="flex-1 flex items-center gap-[2px] h-7 cursor-pointer" onClick={(e) => { const rect = e.currentTarget.getBoundingClientRect(); const pct = ((e.clientX - rect.left) / rect.width) * 100; setProgress(Math.max(0, Math.min(100, pct))); }}>
          {Array.from({ length: 28 }, (_, i) => {
            const heights = [10, 18, 24, 14, 26, 12, 20, 16, 28, 12, 22, 9, 18, 25, 11, 19, 14, 23, 10, 16, 21, 13, 27, 15, 20, 11, 17, 24];
            const pct = ((i + 1) / 28) * 100;
            const played = pct <= progress;
            return <span key={i} className="flex-1 rounded-sm transition-colors" style={{ height: `${heights[i % heights.length]}px`, backgroundColor: played ? theme.primary : theme.rule }} />;
          })}
        </div>
        <span className="text-xs font-mono shrink-0" style={{ color: theme.inkMuted }}>
          {formatTime(Math.round((progress / 100) * call.durationSec))} / {formatTime(call.durationSec)}
        </span>
      </div>
      <div className="flex justify-between items-center text-xs" style={{ color: theme.inkMuted }}>
        <div className="flex gap-5">
          <span className="uppercase tracking-wide font-bold">Bot: <span className="ml-1 normal-case" style={{ color: theme.primary }}>{call.bot}</span></span>
          <span className="uppercase tracking-wide font-bold">Avg Turn: <span className={`font-mono ml-1 ${call.avgLatencyMs <= 300 ? 'text-green-600 dark:text-green-400' : 'text-yellow-600 dark:text-yellow-400'}`}>{call.avgLatencyMs}ms</span></span>
        </div>
        {/* Item 39: data residency cue */}
        <span className="text-[10px]" style={{ color: theme.inkMuted }}>Recording · S3 ap-south-1, 30-day retention</span>
      </div>
    </div>
  );
}
