// Pure, framework-free model for the conversation-flow editor (roadmap 6.6).
//
// Kept React-free on purpose: App.tsx imports `coerceFlow` on the hydration path,
// so this must not drag the canvas/inspector components into the eager bundle.
// Every mutation returns a NEW Flow (immutable) and is reference-safe — renaming
// or deleting a node rewrites every branch that pointed at it.

import type { Flow, FlowNode, FlowNodeUi, FlowBranchKey } from '../../types';

// Intent values mirror the backend `Intent` enum (koel/nlp/schemas.py). `'*'` is
// the wildcard branch, taken when no explicit intent branch matches.
export const FLOW_INTENTS = [
  'high_interest', 'interested', 'objection', 'not_interested', 'busy',
  'promise_to_pay', 'dispute', 'request_human', 'opt_out', 'smalltalk', 'unknown',
] as const;

export const WILDCARD = '*';

export const BRANCH_KEYS: FlowBranchKey[] = [...FLOW_INTENTS, WILDCARD];

// Human-readable labels for branch dropdowns + edge chips.
const BRANCH_LABEL: Record<string, string> = {
  high_interest: 'High interest',
  interested: 'Interested',
  objection: 'Objection',
  not_interested: 'Not interested',
  busy: 'Busy',
  promise_to_pay: 'Promise to pay',
  dispute: 'Dispute',
  request_human: 'Request human',
  opt_out: 'Opt out',
  smalltalk: 'Small talk',
  unknown: 'Unknown',
  '*': 'Any (fallback)',
};

export function branchLabel(key: string): string {
  return BRANCH_LABEL[key] ?? key;
}

// Node card footprint on the board — drives edge geometry + auto-layout.
export const NODE_W = 190;
export const NODE_H = 92;
const COL_GAP = 80;
const ROW_GAP = 48;
const ORIGIN = 44;

// ── Auto-layout ──────────────────────────────────────────────────────────────
// A layered left-to-right layout: BFS depth from `start` picks the column, order
// within a column stacks rows. Used only to seed positions for flows authored
// without the editor (e.g. a backend seed); the editor then persists drags.
function autoLayout(flow: Flow): Record<string, FlowNodeUi> {
  const depth: Record<string, number> = {};
  const order: string[] = [];
  const queue: string[] = [];
  if (flow.nodes[flow.start]) {
    depth[flow.start] = 0;
    queue.push(flow.start);
  }
  while (queue.length) {
    const id = queue.shift();
    if (id === undefined) break;
    const node = flow.nodes[id];
    if (!node) continue;
    for (const target of Object.values(node.branches)) {
      if (flow.nodes[target] && depth[target] === undefined) {
        depth[target] = depth[id] + 1;
        queue.push(target);
      }
    }
  }
  let maxDepth = 0;
  for (const d of Object.values(depth)) maxDepth = Math.max(maxDepth, d);
  // Nodes unreachable from start get parked in a trailing column.
  for (const id of Object.keys(flow.nodes)) {
    if (depth[id] === undefined) depth[id] = maxDepth + 1;
  }
  // Stable order: start-reachable BFS order first, then any remaining ids.
  const reachedOrder = Object.keys(flow.nodes).sort((a, b) => depth[a] - depth[b]);
  const rowByCol: Record<number, number> = {};
  const pos: Record<string, FlowNodeUi> = {};
  for (const id of reachedOrder) {
    order.push(id);
    const col = depth[id];
    const row = rowByCol[col] ?? 0;
    rowByCol[col] = row + 1;
    pos[id] = { x: ORIGIN + col * (NODE_W + COL_GAP), y: ORIGIN + row * (NODE_H + ROW_GAP) };
  }
  return pos;
}

// Fill `_ui` for any node missing a position (idempotent — a fully-positioned
// flow is returned unchanged so it never churns the dirty-state comparison).
export function ensurePositions(flow: Flow): Flow {
  const missing = Object.values(flow.nodes).some((n) => !n._ui);
  if (!missing) return flow;
  const layout = autoLayout(flow);
  const nodes: Record<string, FlowNode> = {};
  for (const [id, node] of Object.entries(flow.nodes)) {
    nodes[id] = node._ui ? node : { ...node, _ui: layout[id] ?? { x: ORIGIN, y: ORIGIN } };
  }
  return { ...flow, nodes };
}

// ── Coercion ───────────────────────────────────────────────────────────────
// Defensively parse an untrusted `config.flow` blob (unknown shape) into a Flow,
// or null when it isn't usable. Mirrors the backend parser's tolerance: unknown
// keys ignored, non-string prompt → "", only string→string branches kept.
export function coerceFlow(raw: unknown): Flow | null {
  if (!raw || typeof raw !== 'object') return null;
  const obj = raw as Record<string, unknown>;
  const nodesRaw = obj.nodes;
  if (!nodesRaw || typeof nodesRaw !== 'object') return null;

  const nodes: Record<string, FlowNode> = {};
  for (const [id, value] of Object.entries(nodesRaw as Record<string, unknown>)) {
    if (!id || !value || typeof value !== 'object') continue;
    const nv = value as Record<string, unknown>;
    const prompt = typeof nv.prompt === 'string' ? nv.prompt : '';
    const branches: Record<string, string> = {};
    if (nv.branches && typeof nv.branches === 'object') {
      for (const [k, target] of Object.entries(nv.branches as Record<string, unknown>)) {
        if (typeof target === 'string') branches[k] = target;
      }
    }
    let ui: FlowNodeUi | undefined;
    if (nv._ui && typeof nv._ui === 'object') {
      const u = nv._ui as Record<string, unknown>;
      if (typeof u.x === 'number' && typeof u.y === 'number') ui = { x: u.x, y: u.y };
    }
    nodes[id] = ui ? { prompt, branches, _ui: ui } : { prompt, branches };
  }
  if (Object.keys(nodes).length === 0) return null;

  const rawStart = typeof obj.start === 'string' ? obj.start : '';
  const start = rawStart && nodes[rawStart] ? rawStart : Object.keys(nodes)[0];
  return ensurePositions({ start, nodes });
}

// A minimal, valid starter flow (single greeting node) for bots with no flow yet.
export function defaultFlow(): Flow {
  return {
    start: 'greeting',
    nodes: {
      greeting: {
        prompt:
          'Greet the customer by name, state that this is an automated assistant, and ask if they have a minute to talk.',
        branches: {},
        _ui: { x: ORIGIN, y: ORIGIN },
      },
    },
  };
}

// ── Validation (non-blocking, surfaced inline) ───────────────────────────────
export interface FlowIssue {
  level: 'error' | 'warn';
  message: string;
  nodeId?: string;
}

// Node ids reachable from `start` by following existing branch targets.
export function reachableIds(flow: Flow): Set<string> {
  const seen = new Set<string>();
  if (!flow.nodes[flow.start]) return seen;
  const queue = [flow.start];
  seen.add(flow.start);
  while (queue.length) {
    const id = queue.shift();
    if (id === undefined) break;
    const node = flow.nodes[id];
    if (!node) continue;
    for (const target of Object.values(node.branches)) {
      if (flow.nodes[target] && !seen.has(target)) {
        seen.add(target);
        queue.push(target);
      }
    }
  }
  return seen;
}

export function validateFlow(flow: Flow): FlowIssue[] {
  const issues: FlowIssue[] = [];
  const ids = Object.keys(flow.nodes);
  if (ids.length === 0) {
    issues.push({ level: 'error', message: 'The flow has no nodes yet — add one to begin.' });
    return issues;
  }
  if (!flow.start) {
    issues.push({ level: 'error', message: 'No start node is set — mark a node as the start.' });
  } else if (!flow.nodes[flow.start]) {
    issues.push({ level: 'error', message: `Start node "${flow.start}" does not exist.` });
  }
  for (const [id, node] of Object.entries(flow.nodes)) {
    for (const [key, target] of Object.entries(node.branches)) {
      if (!flow.nodes[target]) {
        issues.push({
          level: 'error',
          nodeId: id,
          message: `"${id}" · ${branchLabel(key)} → missing node "${target}".`,
        });
      }
    }
  }
  const reachable = reachableIds(flow);
  for (const id of ids) {
    if (!reachable.has(id)) {
      issues.push({ level: 'warn', nodeId: id, message: `"${id}" is unreachable from the start node.` });
    }
  }
  return issues;
}

// ── Immutable mutations ──────────────────────────────────────────────────────
function uniqueId(flow: Flow, base: string): string {
  if (!flow.nodes[base]) return base;
  let i = 2;
  while (flow.nodes[`${base}_${i}`]) i += 1;
  return `${base}_${i}`;
}

export function isIdAvailable(flow: Flow, id: string, exceptId: string): boolean {
  const trimmed = id.trim();
  if (!trimmed) return false;
  if (trimmed === exceptId) return true;
  return !flow.nodes[trimmed];
}

export function addNode(flow: Flow, at?: FlowNodeUi): { flow: Flow; id: string } {
  const id = uniqueId(flow, 'node');
  const count = Object.keys(flow.nodes).length;
  const position: FlowNodeUi = at ?? {
    x: ORIGIN + (count % 4) * (NODE_W + 40),
    y: ORIGIN + Math.floor(count / 4) * (NODE_H + 60),
  };
  const node: FlowNode = { prompt: '', branches: {}, _ui: position };
  const nodes = { ...flow.nodes, [id]: node };
  // The first-ever node becomes the start automatically.
  const start = flow.start && flow.nodes[flow.start] ? flow.start : id;
  return { flow: { start, nodes }, id };
}

export function deleteNode(flow: Flow, id: string): Flow {
  const nodes: Record<string, FlowNode> = {};
  for (const [nid, node] of Object.entries(flow.nodes)) {
    if (nid === id) continue;
    const branches: Record<string, string> = {};
    for (const [key, target] of Object.entries(node.branches)) {
      if (target !== id) branches[key] = target; // drop branches that pointed at the deleted node
    }
    nodes[nid] = { ...node, branches };
  }
  const remaining = Object.keys(nodes);
  const start = flow.start === id ? (remaining[0] ?? '') : flow.start;
  return { start, nodes };
}

export function renameNode(flow: Flow, oldId: string, newId: string): Flow {
  const trimmed = newId.trim();
  if (oldId === trimmed || !flow.nodes[oldId] || !trimmed || flow.nodes[trimmed]) return flow;
  const nodes: Record<string, FlowNode> = {};
  for (const [id, node] of Object.entries(flow.nodes)) {
    const branches: Record<string, string> = {};
    for (const [key, target] of Object.entries(node.branches)) {
      branches[key] = target === oldId ? trimmed : target; // repoint references
    }
    nodes[id === oldId ? trimmed : id] = { ...node, branches };
  }
  const start = flow.start === oldId ? trimmed : flow.start;
  return { start, nodes };
}

export function setNodePrompt(flow: Flow, id: string, prompt: string): Flow {
  const node = flow.nodes[id];
  if (!node) return flow;
  return { ...flow, nodes: { ...flow.nodes, [id]: { ...node, prompt } } };
}

export function setNodePosition(flow: Flow, id: string, pos: FlowNodeUi): Flow {
  const node = flow.nodes[id];
  if (!node) return flow;
  return { ...flow, nodes: { ...flow.nodes, [id]: { ...node, _ui: pos } } };
}

export function setBranches(flow: Flow, id: string, branches: Record<string, string>): Flow {
  const node = flow.nodes[id];
  if (!node) return flow;
  return { ...flow, nodes: { ...flow.nodes, [id]: { ...node, branches } } };
}

export function setStart(flow: Flow, id: string): Flow {
  if (!flow.nodes[id]) return flow;
  return { ...flow, start: id };
}
