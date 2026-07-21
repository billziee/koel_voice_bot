import { useState } from 'react';
import { AlertTriangle, CheckCircle2, Info, Sparkles } from 'lucide-react';

import type { Flow, FlowNodeUi } from '../../types';
import type { Theme } from '../../lib/theme';
import { FlowCanvas } from './FlowCanvas';
import { NodeInspector } from './NodeInspector';
import {
  addNode, coerceFlow, deleteNode, renameNode, setBranches, setNodePosition, setNodePrompt, setStart, validateFlow,
} from './flowModel';

interface FlowEditorProps {
  value: Flow;
  onChange: (flow: Flow) => void;
  theme: Theme;
  // Auto node-builder (6.6): when provided, an "Auto-build from knowledge base"
  // button appears. It should return the raw generated flow (or null); the editor
  // coerces + loads it. Omit to hide the button (e.g. no RAG index configured).
  onAutoBuild?: () => Promise<unknown>;
}

// Controlled editor: the parent owns the Flow, this component owns only the
// canvas selection. The canvas and form stay in sync because both read `value`
// and both write through `onChange` — a form edit redraws the canvas, a canvas
// click drives the form.
export function FlowEditor({ value, onChange, theme, onAutoBuild }: FlowEditorProps) {
  const [rawSelected, setRawSelected] = useState<string | null>(() => value.start || Object.keys(value.nodes)[0] || null);
  const [building, setBuilding] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);

  const handleAutoBuild = async () => {
    if (!onAutoBuild || building) return;
    setBuilding(true);
    setBuildError(null);
    try {
      const coerced = coerceFlow(await onAutoBuild());
      if (coerced) {
        onChange(coerced);
        setRawSelected(coerced.start);
      } else {
        setBuildError('The generated flow could not be parsed — try again or build manually.');
      }
    } catch (err) {
      setBuildError(err instanceof Error ? err.message : 'Flow generation failed.');
    } finally {
      setBuilding(false);
    }
  };

  // Effective selection: fall back if the raw selection was renamed/deleted.
  const selectedId =
    rawSelected && value.nodes[rawSelected]
      ? rawSelected
      : value.start && value.nodes[value.start]
        ? value.start
        : Object.keys(value.nodes)[0] ?? null;

  const issues = validateFlow(value);
  const issueNodeIds = new Set<string>();
  for (const issue of issues) if (issue.nodeId) issueNodeIds.add(issue.nodeId);
  const errors = issues.filter((i) => i.level === 'error');
  const warnings = issues.filter((i) => i.level === 'warn');

  const handleAdd = () => {
    const result = addNode(value);
    onChange(result.flow);
    setRawSelected(result.id);
  };
  const handleMove = (id: string, pos: FlowNodeUi) => onChange(setNodePosition(value, id, pos));
  const handleDelete = (id: string) => {
    onChange(deleteNode(value, id));
    setRawSelected(null);
  };

  const selectedNode = selectedId ? value.nodes[selectedId] : undefined;

  return (
    <div>
      {onAutoBuild && (
        <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
          <p className="text-[12px]" style={{ color: theme.faint }}>
            Draft a flow automatically from this bot's knowledge base, then refine it below.
          </p>
          <button
            type="button"
            onClick={handleAutoBuild}
            disabled={building}
            className="flex items-center gap-1.5 text-[12.5px] font-medium px-3 py-1.5 rounded-md shrink-0"
            style={{ backgroundColor: theme.primaryLight, color: theme.primary, opacity: building ? 0.6 : 1 }}
          >
            <Sparkles size={14} /> {building ? 'Building…' : 'Auto-build from knowledge base'}
          </button>
        </div>
      )}
      {buildError && (
        <div className="flex items-start gap-2 text-[12px] mb-3 rounded-md p-2"
             style={{ color: theme.destructive, backgroundColor: theme.inset }}>
          <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} /> <span>{buildError}</span>
        </div>
      )}
      <div className="flex flex-col lg:flex-row gap-4">
        <div className="flex-1 min-w-0">
          <FlowCanvas
            flow={value}
            selectedId={selectedId}
            issueNodeIds={issueNodeIds}
            onSelect={setRawSelected}
            onMove={handleMove}
            onAdd={handleAdd}
            theme={theme}
          />
          <p className="text-[11px] mt-1.5" style={{ color: theme.faint }}>
            Drag a node to reposition · click to edit · positions save with the flow (the backend ignores them).
          </p>
        </div>

        <div className="w-full lg:w-[320px] shrink-0">
          {selectedId && selectedNode ? (
            <NodeInspector
              key={selectedId}
              flow={value}
              nodeId={selectedId}
              node={selectedNode}
              isStart={selectedId === value.start}
              onRename={(newId) => { onChange(renameNode(value, selectedId, newId)); setRawSelected(newId); }}
              onPromptChange={(prompt) => onChange(setNodePrompt(value, selectedId, prompt))}
              onBranchesChange={(branches) => onChange(setBranches(value, selectedId, branches))}
              onSetStart={() => onChange(setStart(value, selectedId))}
              onDelete={() => handleDelete(selectedId)}
              theme={theme}
            />
          ) : (
            <div className="rounded-lg border p-6 text-center" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
              <p className="text-[12.5px]" style={{ color: theme.faint }}>Select a node to edit its prompt and branches.</p>
            </div>
          )}
        </div>
      </div>

      {/* Inline, non-blocking validation */}
      <div className="mt-3 rounded-lg border p-3" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset }}>
        {issues.length === 0 ? (
          <div className="flex items-center gap-2 text-[12px]" style={{ color: theme.green }}>
            <CheckCircle2 size={14} /> Flow is valid — start node set, every branch resolves, all nodes reachable.
          </div>
        ) : (
          <div className="space-y-1.5">
            {errors.map((issue, i) => (
              <div key={`e${i}`} className="flex items-start gap-2 text-[12px]" style={{ color: theme.destructive }}>
                <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} /> <span>{issue.message}</span>
              </div>
            ))}
            {warnings.map((issue, i) => (
              <div key={`w${i}`} className="flex items-start gap-2 text-[12px]" style={{ color: theme.warning }}>
                <Info size={13} style={{ flexShrink: 0, marginTop: 1 }} /> <span>{issue.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
