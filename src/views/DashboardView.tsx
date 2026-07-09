import { useState } from 'react';
import { Search, Plus } from 'lucide-react';

import type { Bot } from '../types';
import type { Theme } from '../lib/theme';

interface DashboardViewProps { bots: Bot[]; onLaunch: (id: string) => void; onConfig: (id: string) => void; onBuilder: () => void; theme: Theme }

export function DashboardView({ bots, onLaunch, onBuilder, theme }: DashboardViewProps) {
  const [sortBy] = useState<string>('calls');
  const [search, setSearch] = useState('');

  const filteredBots = bots.filter(
    b => !search || `${b.name} ${b.useCase}`.toLowerCase().includes(search.toLowerCase())
  );
  const sortedBots = [...filteredBots].sort((a, b) => {
    if (sortBy === 'calls') return b.calls - a.calls;
    if (sortBy === 'conversion') return b.conversionNum - a.conversionNum;
    if (sortBy === 'name') return a.name.localeCompare(b.name);
    return 0;
  });

  const glyphColors: Record<string, { bg: string; fg: string }> = {
    b1: { bg: theme.primaryLight, fg: theme.primary },
    b2: { bg: theme.greenLight, fg: theme.green },
    b3: { bg: theme.accentLight, fg: '#B23A14' },
    b4: { bg: theme.primaryLight, fg: theme.primary },
  };

  // Live KPI aggregates (Q-3): derived from the same per-bot stats the cards use
  // — real `/v1/stats` aggregates when connected to the backend, the shown demo
  // bots otherwise — so the strip always reflects real data, never hardcoded
  // numbers. Rates are weighted by each bot's call volume for a true overall.
  const kpiTotalCalls = bots.reduce((sum, b) => sum + (b.calls || 0), 0);
  const kpiConvPer100 = kpiTotalCalls
    ? bots.reduce((sum, b) => sum + (b.calls || 0) * (b.conversionNum || 0), 0) / kpiTotalCalls
    : 0;
  const kpiEscPct = kpiTotalCalls
    ? bots.reduce((sum, b) => {
        const rate = parseFloat(b.escalationRate); // "9.1%" → 9.1; "—" → NaN (skipped)
        return sum + (Number.isFinite(rate) ? (b.calls || 0) * rate : 0);
      }, 0) / kpiTotalCalls
    : 0;

  return (
    <div className="p-5 pb-8 animate-in fade-in duration-300" style={{ maxWidth: 1200, margin: '0 auto' }}>
      <div className="flex flex-wrap justify-between items-end mb-4 gap-4">
        <div>
          <h1 className="text-[19px] font-semibold tracking-tight" style={{ color: theme.ink }}>Bot Dashboard</h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.inkMuted }}>{bots.filter(b => b.status === 'Active').length} active bot instances · last sync 2 min ago</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: theme.faint }} />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search bots…"
              className="border rounded-md pl-9 pr-3 py-2 text-[13px] outline-none focus:ring-2 w-40 md:w-56"
              style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}
            />
          </div>
          <button onClick={onBuilder} className="flex items-center gap-2 px-3.5 py-[7px] rounded-md text-[13.5px] text-white font-normal transition-colors hover:opacity-90" style={{ backgroundColor: theme.primary }}>
            <Plus size={15} /> New bot
          </button>
        </div>
      </div>

      {/* KPI strip — 4 cards matching design */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3.5 mb-5">
        <KpiCard label="Connected calls" value={kpiTotalCalls.toLocaleString()} meta={`${bots.length} bots`} metaDir="flat" theme={theme} />
        <KpiCard label="First-audio · P95" value="2.3" unit="s" meta="measured baseline · perceived <300ms via filler (SLO.md)" metaDir="flat" theme={theme} />
        <KpiCard label="Warm leads / 100 calls" value={kpiConvPer100.toFixed(1)} meta="conversion across bots" metaDir="flat" theme={theme} />
        <KpiCard label="Escalation rate" value={`${kpiEscPct.toFixed(1)}%`} meta="100% with full context handoff" metaDir="flat" theme={theme} />
      </div>

      {search && sortedBots.length === 0 && (
        <div className="text-[13px] text-center py-8" style={{ color: theme.inkMuted }}>
          No bots match “{search}”.
        </div>
      )}

      {/* Bot grid — 3 columns */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3.5">
        {sortedBots.map(bot => {
          const gc = glyphColors[bot.id] || { bg: theme.primaryLight, fg: theme.primary };
          return (
            <div key={bot.id} onClick={() => onLaunch(bot.id)} className="border rounded-lg p-4 flex flex-col gap-3 cursor-pointer transition-colors" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }} onMouseEnter={e => (e.currentTarget.style.borderColor = theme.primary)} onMouseLeave={e => (e.currentTarget.style.borderColor = theme.rule)}>
              <div className="flex items-start justify-between gap-2.5">
                <div className="flex items-center gap-2.5 min-w-0">
                  <div className="w-9 h-9 rounded-md flex items-center justify-center flex-none text-[15px] font-medium" style={{ backgroundColor: gc.bg, color: gc.fg }}>{bot.name[0]}</div>
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate" style={{ color: theme.ink }}>{bot.name}</div>
                    <div className="text-xs mt-0.5" style={{ color: theme.inkMuted }}>{bot.useCase} · Outgoing</div>
                  </div>
                </div>
                {bot.status === 'Active' ? (
                  <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11.5px] font-medium whitespace-nowrap" style={{ backgroundColor: theme.greenLight, color: theme.green }}>
                    <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: 'currentColor' }}></span>Live
                  </span>
                ) : (
                  <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11.5px] font-medium whitespace-nowrap border" style={{ backgroundColor: theme.inset, color: theme.inkMuted, borderColor: theme.ruleSubtle }}>Paused</span>
                )}
              </div>

              {/* Stat row — calls, conversion, avg duration, escalation (PRD §09 Screen 1) */}
              <div className="grid grid-cols-4 rounded-md overflow-hidden border" style={{ borderColor: theme.ruleSubtle, gap: 1, backgroundColor: theme.ruleSubtle }}>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Calls</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.calls.toLocaleString()}</div>
                </div>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Conv.</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.conversion}</div>
                </div>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Avg dur</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.avgDuration}</div>
                </div>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Esc.</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.escalationRate}</div>
                </div>
              </div>

              {/* Footer */}
              <div className="flex justify-between items-center text-xs" style={{ color: theme.faint }}>
                <div className="flex gap-1">
                  {bot.language.split(/[,/]/).map(l => l.trim()).map(l => (
                    <span key={l} className="px-[7px] py-px rounded-full border text-[10.5px]" style={{ borderColor: theme.rule, color: theme.inkMuted }}>{l}</span>
                  ))}
                </div>
                <span>Active {bot.lastActive}</span>
              </div>
            </div>
          );
        })}

        {/* Add new bot card */}
        <button onClick={onBuilder} className="rounded-lg flex flex-col items-center justify-center gap-2 min-h-[170px] cursor-pointer transition-colors" style={{ border: '1.5px dashed ' + theme.rule, color: theme.inkMuted, background: 'transparent' }} onMouseEnter={e => { e.currentTarget.style.borderColor = theme.primary; e.currentTarget.style.color = theme.primary; e.currentTarget.style.backgroundColor = theme.primarySubtle; }} onMouseLeave={e => { e.currentTarget.style.borderColor = theme.rule; e.currentTarget.style.color = theme.inkMuted; e.currentTarget.style.backgroundColor = 'transparent'; }}>
          <div className="w-[34px] h-[34px] rounded-full border-[1.5px] flex items-center justify-center" style={{ borderColor: 'currentColor' }}>
            <Plus size={15} />
          </div>
          <span className="text-[13px] font-medium">Add new bot</span>
          <span className="text-[11.5px]" style={{ color: theme.faint }}>Form-based setup · no dev work required</span>
        </button>
      </div>
    </div>
  );
}

function KpiCard({ label, value, unit, meta, metaDir, theme }: { label: string; value: string; unit?: string; meta: string; metaDir: 'up' | 'down' | 'flat'; theme: Theme }) {
  return (
    <div className="border rounded-lg px-4 py-3.5" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <p className="text-xs" style={{ color: theme.inkMuted }}>{label}</p>
      <p className="text-2xl font-semibold tracking-tight mt-1" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>
        {value}{unit && <span className="text-sm font-normal ml-0.5" style={{ color: theme.inkMuted }}>{unit}</span>}
      </p>
      <p className="text-[11.5px] mt-1 flex items-center gap-1.5" style={{ color: metaDir === 'up' ? theme.green : metaDir === 'down' ? theme.destructive : theme.faint }}>
        {metaDir === 'up' && <>&#9650; {meta}</>}
        {metaDir === 'down' && <>&#9660; {meta}</>}
        {metaDir === 'flat' && meta}
      </p>
    </div>
  );
}
