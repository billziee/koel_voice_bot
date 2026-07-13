import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, GitBranch, Plus } from 'lucide-react';

import type { Flow, FlowNodeUi } from '../../types';
import type { Theme } from '../../lib/theme';
import { NODE_W, NODE_H, branchLabel } from './flowModel';

interface FlowCanvasProps {
  flow: Flow;
  selectedId: string | null;
  issueNodeIds: Set<string>;
  onSelect: (id: string) => void;
  onMove: (id: string, pos: FlowNodeUi) => void;
  onAdd: () => void;
  theme: Theme;
}

interface Edge {
  key: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  midX: number;
  midY: number;
  label: string;
  fromSelected: boolean;
  wildcard: boolean;
  selfLoop: boolean;
  path?: string;
}

const CANVAS_H = 420;
const DRAG_THRESHOLD = 4;

// Point on the border of a card (centre cx,cy, size w×h) in the direction of
// (px,py) — used to anchor edge ends on the card edge, not its centre.
function borderPoint(cx: number, cy: number, w: number, h: number, px: number, py: number): FlowNodeUi {
  const dx = px - cx;
  const dy = py - cy;
  if (dx === 0 && dy === 0) return { x: cx, y: cy };
  const scale = 1 / Math.max(Math.abs(dx) / (w / 2), Math.abs(dy) / (h / 2));
  return { x: cx + dx * scale, y: cy + dy * scale };
}

export function FlowCanvas({ flow, selectedId, issueNodeIds, onSelect, onMove, onAdd, theme }: FlowCanvasProps) {
  const drag = useRef<{ id: string; startX: number; startY: number; ox: number; oy: number; moved: boolean } | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);

  // Keep the latest callbacks in refs so the window listeners stay stable.
  const onMoveRef = useRef(onMove);
  onMoveRef.current = onMove;
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  const handleMove = useCallback((e: PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (!d.moved && Math.abs(dx) + Math.abs(dy) > DRAG_THRESHOLD) d.moved = true;
    if (d.moved) {
      onMoveRef.current(d.id, { x: Math.max(0, Math.round(d.ox + dx)), y: Math.max(0, Math.round(d.oy + dy)) });
    }
  }, []);

  const handleUp = useCallback(() => {
    const d = drag.current;
    window.removeEventListener('pointermove', handleMove);
    window.removeEventListener('pointerup', handleUp);
    drag.current = null;
    setDragId(null);
    if (d && !d.moved) onSelectRef.current(d.id); // a click that never moved = select
  }, [handleMove]);

  useEffect(() => () => {
    window.removeEventListener('pointermove', handleMove);
    window.removeEventListener('pointerup', handleUp);
  }, [handleMove, handleUp]);

  const handleDown = (e: React.PointerEvent, id: string) => {
    e.preventDefault();
    const pos = flow.nodes[id]?._ui ?? { x: 0, y: 0 };
    drag.current = { id, startX: e.clientX, startY: e.clientY, ox: pos.x, oy: pos.y, moved: false };
    setDragId(id);
    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
  };

  const entries = Object.entries(flow.nodes);

  // Board extent — grows to fit the furthest node, with a comfortable margin.
  let boardW = 640;
  let boardH = CANVAS_H;
  for (const [, node] of entries) {
    const pos = node._ui ?? { x: 0, y: 0 };
    boardW = Math.max(boardW, pos.x + NODE_W + 80);
    boardH = Math.max(boardH, pos.y + NODE_H + 80);
  }

  const centre = (id: string): FlowNodeUi => {
    const p = flow.nodes[id]?._ui ?? { x: 0, y: 0 };
    return { x: p.x + NODE_W / 2, y: p.y + NODE_H / 2 };
  };

  // Build edges (skip branch targets that don't exist — the inspector flags those).
  const edges: Edge[] = [];
  for (const [id, node] of entries) {
    for (const [key, target] of Object.entries(node.branches)) {
      if (!flow.nodes[target]) continue;
      const fromSelected = id === selectedId;
      const wildcard = key === '*';
      if (target === id) {
        const p = node._ui ?? { x: 0, y: 0 };
        const sx = p.x + NODE_W;
        const sy = p.y + NODE_H / 2;
        edges.push({
          key: `${id}-${key}`,
          x1: sx, y1: sy, x2: sx, y2: sy,
          midX: sx + 46, midY: sy,
          label: branchLabel(key), fromSelected, wildcard, selfLoop: true,
          path: `M ${sx} ${sy - 8} C ${sx + 56} ${sy - 26}, ${sx + 56} ${sy + 26}, ${sx} ${sy + 8}`,
        });
        continue;
      }
      const c1 = centre(id);
      const c2 = centre(target);
      const a = borderPoint(c1.x, c1.y, NODE_W, NODE_H, c2.x, c2.y);
      const b = borderPoint(c2.x, c2.y, NODE_W, NODE_H, c1.x, c1.y);
      edges.push({
        key: `${id}-${key}`,
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        midX: (a.x + b.x) / 2, midY: (a.y + b.y) / 2,
        label: branchLabel(key), fromSelected, wildcard, selfLoop: false,
      });
    }
  }

  const dotGrid = `radial-gradient(${theme.rule} 1px, transparent 1px)`;

  return (
    <div
      className="relative overflow-auto rounded-lg border"
      style={{ borderColor: theme.rule, backgroundColor: theme.inset, height: CANVAS_H }}
    >
      {/* Floating add-node control */}
      <button
        type="button"
        onClick={onAdd}
        className="absolute top-2.5 right-2.5 z-20 flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium border shadow-sm transition-colors hover:opacity-90"
        style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.primary }}
      >
        <Plus size={13} /> Add node
      </button>

      {entries.length === 0 && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center px-6">
          <GitBranch size={28} style={{ color: theme.faint }} />
          <p className="text-[13px] font-medium" style={{ color: theme.inkMuted }}>No nodes yet</p>
          <p className="text-[11.5px] max-w-xs" style={{ color: theme.faint }}>Add a node to start scripting how KOEL moves through the call.</p>
        </div>
      )}

      <div
        className="relative"
        style={{ width: boardW, height: boardH, backgroundImage: dotGrid, backgroundSize: '22px 22px', backgroundPosition: '11px 11px' }}
      >
        <svg width={boardW} height={boardH} className="absolute inset-0" style={{ pointerEvents: 'none' }}>
          <defs>
            <marker id="koel-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L7,3 L0,6 Z" fill={theme.inkMuted} />
            </marker>
            <marker id="koel-arrow-pri" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L7,3 L0,6 Z" fill={theme.primary} />
            </marker>
          </defs>
          {edges.map((e) => {
            const stroke = e.fromSelected ? theme.primary : theme.inkMuted;
            const marker = e.fromSelected ? 'url(#koel-arrow-pri)' : 'url(#koel-arrow)';
            if (e.selfLoop && e.path) {
              return (
                <path
                  key={e.key}
                  d={e.path}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={1.5}
                  strokeDasharray={e.wildcard ? '4 3' : undefined}
                  markerEnd={marker}
                  opacity={e.fromSelected ? 1 : 0.7}
                />
              );
            }
            return (
              <line
                key={e.key}
                x1={e.x1}
                y1={e.y1}
                x2={e.x2}
                y2={e.y2}
                stroke={stroke}
                strokeWidth={1.5}
                strokeDasharray={e.wildcard ? '4 3' : undefined}
                markerEnd={marker}
                opacity={e.fromSelected ? 1 : 0.7}
              />
            );
          })}
        </svg>

        {/* Edge labels (HTML chips, so they pick up theme + font) */}
        {edges.map((e) => (
          <span
            key={`lbl-${e.key}`}
            className="absolute -translate-x-1/2 -translate-y-1/2 px-1.5 py-0.5 rounded text-[9.5px] font-medium whitespace-nowrap"
            style={{
              left: e.midX,
              top: e.midY,
              pointerEvents: 'none',
              backgroundColor: e.fromSelected ? theme.primaryLight : theme.surfaceCard,
              color: e.fromSelected ? theme.primary : theme.inkMuted,
              border: `1px solid ${e.fromSelected ? theme.primary : theme.rule}`,
            }}
          >
            {e.label}
          </span>
        ))}

        {/* Node cards */}
        {entries.map(([id, node]) => {
          const pos = node._ui ?? { x: 0, y: 0 };
          const isStart = id === flow.start;
          const isSelected = id === selectedId;
          const hasIssue = issueNodeIds.has(id);
          const branchCount = Object.keys(node.branches).length;
          return (
            <div
              key={id}
              onPointerDown={(ev) => handleDown(ev, id)}
              className="absolute select-none rounded-lg border transition-shadow"
              style={{
                left: pos.x,
                top: pos.y,
                width: NODE_W,
                minHeight: NODE_H,
                backgroundColor: theme.surfaceCard,
                borderColor: isSelected ? theme.primary : theme.rule,
                borderTopWidth: isStart ? 3 : 1,
                borderTopColor: isStart ? theme.primary : isSelected ? theme.primary : theme.rule,
                boxShadow: isSelected ? `0 0 0 2px ${theme.primaryLight}, 0 4px 12px rgba(0,0,0,0.08)` : '0 1px 2px rgba(0,0,0,0.05)',
                cursor: dragId === id ? 'grabbing' : 'grab',
                touchAction: 'none',
              }}
            >
              <div className="px-3 pt-2 pb-2.5">
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[12.5px] font-semibold truncate" style={{ color: theme.ink, fontFamily: '"DM Mono", monospace' }}>{id}</span>
                  {isStart ? (
                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>Start</span>
                  ) : hasIssue ? (
                    <AlertTriangle size={12} style={{ color: theme.warning, flexShrink: 0 }} />
                  ) : null}
                </div>
                <p className="text-[11px] leading-snug line-clamp-2" style={{ color: node.prompt ? theme.inkMuted : theme.faint }}>
                  {node.prompt || 'No prompt guidance'}
                </p>
                <div className="mt-1.5 flex items-center gap-1 text-[10px]" style={{ color: theme.faint }}>
                  <GitBranch size={11} /> {branchCount} branch{branchCount === 1 ? '' : 'es'}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
