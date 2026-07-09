import { Database, Volume2 } from 'lucide-react';

import type { Tone, TranscriptMessageData } from '../types';
import type { Theme } from '../lib/theme';
import { formatTime } from '../lib/format';

// Tone → chip styling. Private to the transcript bubble — it's the only consumer.
const TONE_STYLES: Record<Tone, string> = {
  cheerful: 'bg-[#FFF7E6] text-[#9A6700] border-[#F5D789]',
  empathetic: 'bg-[#F2EAFB] text-[#6B2FB3] border-[#D9C2F0]',
  firm: 'bg-[#FFF0EB] text-[#EB5324] border-[#F5C0AD]',
  neutral: 'bg-gray-100 text-gray-600 border-gray-300',
};

// One transcript bubble — bot/customer message, meta event, or filler marker.
// Shared by the live call view and the call-history replay.
export function TranscriptMessage({ msg, compact, theme }: { msg: TranscriptMessageData; compact?: boolean; theme: Theme }) {
  if (msg.kind === 'meta') {
    return (
      <div className="flex justify-center my-1 animate-in fade-in slide-in-from-bottom-2">
        <span className="px-4 py-1.5 text-[11px] font-bold uppercase tracking-wider rounded-full flex items-center gap-1.5 border shadow-sm text-center max-w-[85%]" style={{ backgroundColor: theme.primarySubtle, color: theme.primary, borderColor: theme.primaryLight }}>
          <Database size={11} className="shrink-0" /> {msg.text}
        </span>
      </div>
    );
  }

  if (msg.kind === 'filler') {
    return (
      <div className="flex justify-start my-1 animate-in fade-in slide-in-from-bottom-2 ml-11">
        <span className="px-4 py-1.5 bg-[#FFF7E6] dark:bg-[#422006]/40 text-[#9A6700] dark:text-yellow-400 text-xs font-medium italic rounded-full flex items-center gap-1.5 border border-[#F5D789] dark:border-yellow-800 shadow-sm">
          <Volume2 size={12} /> Filler audio: {msg.text}
        </span>
      </div>
    );
  }

  const isBot = msg.kind === 'bot';
  return (
    <div className={`flex flex-col max-w-[80%] animate-in fade-in slide-in-from-bottom-2 ${isBot ? 'self-start items-start' : 'self-end items-end'}`}>
      <span className="text-[10px] uppercase font-bold tracking-wider mb-1 mx-1 flex items-center gap-2" style={{ color: theme.inkMuted }}>
        {isBot ? 'KOEL Bot' : 'Customer'}
        {isBot && msg.tone && (
          <span className={`px-1.5 py-px rounded border text-[9px] normal-case font-bold ${TONE_STYLES[msg.tone]}`}>[{msg.tone}]</span>
        )}
        {msg.interrupted && (
          <span className="px-1.5 py-px rounded border text-[9px] normal-case font-bold bg-red-50 text-red-600 border-red-200">interrupted</span>
        )}
        {/* Item 58: per-turn timestamp */}
        {msg.timestamp !== undefined && (
          <span className="font-mono text-[9px] normal-case" style={{ color: theme.inkMuted }}>{formatTime(msg.timestamp)}</span>
        )}
      </span>
      <div className={`flex gap-3 items-end ${isBot ? '' : 'flex-row-reverse'}`}>
        <div className={`${compact ? 'w-6 h-6 text-[9px]' : 'w-8 h-8 text-xs'} rounded-full flex-shrink-0 flex items-center justify-center font-bold text-white shadow-sm ${isBot ? 'bg-[#053BEE]' : 'bg-gray-400'}`}>
          {isBot ? 'K' : 'U'}
        </div>
        <div className={`${compact ? 'px-4 py-3 text-[14px]' : 'p-4 text-[15px]'} rounded-2xl leading-relaxed shadow-sm border ${msg.interrupted ? 'opacity-70' : ''}`}
          style={{
            backgroundColor: isBot ? theme.surfaceCard : theme.primaryLight,
            color: isBot ? theme.ink : theme.primaryDark,
            borderColor: isBot ? theme.rule : theme.primary + '15',
            borderRadius: isBot ? '1rem 1rem 1rem 0.25rem' : '1rem 1rem 0.25rem 1rem',
          }}>
          {msg.text}
        </div>
      </div>
      {isBot && msg.latency && (
        <span className={`text-[10px] font-mono mt-1 ml-11`} style={{ color: msg.latency.total <= 300 ? theme.inkMuted : '#9A6700' }} title="STT / NLP / TTS / total — from OTel spans">
          ⏱ {msg.latency.stt > 0 ? `stt ${msg.latency.stt} · ` : ''}nlp {msg.latency.nlp} · tts {msg.latency.tts} · <b>{msg.latency.total}ms</b>{msg.latency.total > 300 ? ' (over budget → filler)' : ''}
        </span>
      )}
    </div>
  );
}
