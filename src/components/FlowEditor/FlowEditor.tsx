import { useState } from 'react';
import { AlertTriangle, CheckCircle2, Info } from 'lucide-react';

import type { Flow, FlowNodeUi } from '../../types';
import type { Theme } from '../../lib/theme';
import { FlowCanvas } from './FlowCanvas';
import { NodeInspector } from './NodeInspector';
import {
  addNode, deleteNode, renameNode, setBranches, setNodePosition, setNodePrompt, setStart, validateFlow,
} from './flowModel';

interface FlowEditorProps {
  value: Flow;
  onChange: (flow: Flow) => void;
  theme: Theme;
}

// Controlled editor: the parent owns the Flow, this component owns only the
// canvas selection. The canvas and form stay in sync because both read `value`
// and both write through `onChange` — a form edit redraws the canvas, a canvas
// click drives the form.
export function FlowEditor({ value, onChange, theme }: FlowEditorProps) {
  const [rawSelected, setRawSelected] = useState<string | null>(() => value.start || Object.keys(value.nodes)[0] || null);

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
