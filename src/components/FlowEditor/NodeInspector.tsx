import { useState } from 'react';
import { Flag, Plus, Trash2, X } from 'lucide-react';

import type { Flow, FlowNode } from '../../types';
import type { Theme } from '../../lib/theme';
import { BRANCH_KEYS, branchLabel, isIdAvailable } from './flowModel';

interface NodeInspectorProps {
  flow: Flow;
  nodeId: string;
  node: FlowNode;
  isStart: boolean;
  onRename: (newId: string) => void;
  onPromptChange: (prompt: string) => void;
  onBranchesChange: (branches: Record<string, string>) => void;
  onSetStart: () => void;
  onDelete: () => void;
  theme: Theme;
}

// A remounting `key={nodeId}` in the parent resets the id draft on selection
// change, so the local draft never leaks between nodes.
export function NodeInspector({
  flow, nodeId, node, isStart, onRename, onPromptChange, onBranchesChange, onSetStart, onDelete, theme,
}: NodeInspectorProps) {
  const [idDraft, setIdDraft] = useState(nodeId);
  const idOk = isIdAvailable(flow, idDraft, nodeId);

  const commitRename = () => {
    const trimmed = idDraft.trim();
    if (!idOk || trimmed === nodeId) {
      setIdDraft(nodeId); // revert an invalid/duplicate draft
      return;
    }
    onRename(trimmed);
  };

  const usedKeys = Object.keys(node.branches);
  const nodeIds = Object.keys(flow.nodes);
  const firstUnused = BRANCH_KEYS.find((k) => !usedKeys.includes(k));

  const changeIntent = (oldKey: string, newKey: string) => {
    const target = node.branches[oldKey];
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(node.branches)) next[k === oldKey ? newKey : k] = k === oldKey ? target : v;
    onBranchesChange(next);
  };
  const changeTarget = (key: string, target: string) => {
    onBranchesChange({ ...node.branches, [key]: target });
  };
  const removeBranch = (key: string) => {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(node.branches)) if (k !== key) next[k] = v;
    onBranchesChange(next);
  };
  const addBranch = () => {
    if (!firstUnused) return;
    const defaultTarget = nodeIds.find((x) => x !== nodeId) ?? nodeId;
    onBranchesChange({ ...node.branches, [firstUnused]: defaultTarget });
  };

  const labelCls = 'block text-[11px] font-medium mb-1.5 uppercase tracking-wide';
  const inputCls = 'w-full border rounded-md px-2.5 py-1.5 text-[12.5px] outline-none focus:ring-2';
  const inputStyle = { borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' };

  return (
    <div className="rounded-lg border overflow-hidden flex flex-col" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <div className="px-4 py-3 border-b flex items-center justify-between gap-2" style={{ borderColor: theme.ruleSubtle }}>
        <span className="text-[13px] font-semibold" style={{ color: theme.ink }}>Node details</span>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={onSetStart}
            disabled={isStart}
            className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium border transition-colors disabled:opacity-100"
            style={{
              borderColor: isStart ? theme.primary : theme.rule,
              backgroundColor: isStart ? theme.primaryLight : 'transparent',
              color: isStart ? theme.primary : theme.inkMuted,
              cursor: isStart ? 'default' : 'pointer',
            }}
            title={isStart ? 'This is the start node' : 'Mark as the start node'}
          >
            <Flag size={11} /> {isStart ? 'Start node' : 'Set start'}
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="flex items-center justify-center w-7 h-7 rounded-md border transition-colors"
            style={{ borderColor: theme.rule, color: theme.destructive }}
            aria-label="Delete node"
            title="Delete node"
          >
            <Trash2 size={13} />
          </button>
        </div>
      </div>

      <div className="p-4 space-y-4 overflow-y-auto">
        {/* Node id */}
        <div>
          <label className={labelCls} style={{ color: theme.faint }}>Node id</label>
          <input
            value={idDraft}
            onChange={(e) => setIdDraft(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }}
            className={inputCls}
            style={{ ...inputStyle, borderColor: idOk ? theme.rule : theme.destructive, fontFamily: '"DM Mono", monospace' }}
          />
          {!idOk && (
            <p className="text-[10.5px] mt-1" style={{ color: theme.destructive }}>
              {idDraft.trim() ? 'A node with this id already exists.' : 'Id cannot be empty.'}
            </p>
          )}
        </div>

        {/* Prompt */}
        <div>
          <label className={labelCls} style={{ color: theme.faint }}>Turn prompt</label>
          <textarea
            value={node.prompt}
            onChange={(e) => onPromptChange(e.target.value)}
            placeholder="Per-turn guidance for the model on this node…"
            className={`${inputCls} min-h-[84px] resize-y`}
            style={inputStyle}
          />
          <p className="text-[10.5px] mt-1" style={{ color: theme.faint }}>Appended to the system prompt for this turn — it guides, never replaces, the LLM.</p>
        </div>

        {/* Branches */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-[11px] font-medium uppercase tracking-wide" style={{ color: theme.faint }}>Branches</label>
            <button
              type="button"
              onClick={addBranch}
              disabled={!firstUnused}
              className="flex items-center gap-1 text-[11.5px] font-medium disabled:opacity-40"
              style={{ color: theme.primary, cursor: firstUnused ? 'pointer' : 'not-allowed' }}
            >
              <Plus size={12} /> Add branch
            </button>
          </div>

          {usedKeys.length === 0 && (
            <p className="text-[11.5px] py-2" style={{ color: theme.faint }}>No branches — the call holds on this node until one is added.</p>
          )}

          <div className="space-y-2">
            {Object.entries(node.branches).map(([key, target]) => {
              const intentOptions = BRANCH_KEYS.filter((k) => k === key || !usedKeys.includes(k));
              const targetMissing = !flow.nodes[target];
              return (
                <div key={key} className="flex items-center gap-1.5">
                  <select
                    value={key}
                    onChange={(e) => changeIntent(key, e.target.value)}
                    className="flex-1 min-w-0 border rounded-md px-2 py-1.5 text-[12px] outline-none"
                    style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink }}
                  >
                    {intentOptions.map((k) => (
                      <option key={k} value={k}>{branchLabel(k)}</option>
                    ))}
                  </select>
                  <span className="text-[12px] shrink-0" style={{ color: theme.faint }}>→</span>
                  <select
                    value={target}
                    onChange={(e) => changeTarget(key, e.target.value)}
                    className="flex-1 min-w-0 border rounded-md px-2 py-1.5 text-[12px] outline-none"
                    style={{ borderColor: targetMissing ? theme.destructive : theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Mono", monospace' }}
                  >
                    {targetMissing && <option value={target}>{target} (missing)</option>}
                    {nodeIds.map((nid) => (
                      <option key={nid} value={nid}>{nid}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => removeBranch(key)}
                    className="flex items-center justify-center w-6 h-6 rounded shrink-0 transition-colors"
                    style={{ color: theme.faint }}
                    aria-label={`Remove ${branchLabel(key)} branch`}
                  >
                    <X size={13} />
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
