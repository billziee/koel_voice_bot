// Small formatting helpers shared across the dashboard views.
// Extracted from App.tsx (roadmap Q-1 decomposition).

/** Seconds → "MM:SS" (call timers, per-turn timestamps). */
export const formatTime = (seconds: number) => {
  const m = Math.floor(seconds / 60).toString().padStart(2, '0');
  const s = (seconds % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
};

/** Epoch ms → coarse "N sec/min/hr/days ago" (bot "last active" labels). */
export const formatRelativeTime = (ts: number) => {
  const diff = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (diff < 60) return `${diff} sec ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return `${Math.floor(diff / 86400)} days ago`;
};
