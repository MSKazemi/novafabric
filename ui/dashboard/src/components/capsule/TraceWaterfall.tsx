import { useState, useMemo } from 'react';
import { clsx } from 'clsx';
import {
  type RawSpan,
  spanStatusOk,
  buildSpanAgentMap,
  buildAgentColors,
  agentInitial,
  parseTs,
  normalizeModelCall,
  normalizeToolCall,
  propagateAgents,
} from '../../lib/traceUtils';

interface SpanNode {
  id: string;
  name: string;
  durationMs: number;
  kind: string;
  startOffset: number;   // ms from root start
  status: RawSpan['status'];
  agent?: string;
  children: SpanNode[];
  depth: number;
}

function buildTree(
  spans: RawSpan[],
  modelCalls: Record<string, unknown>[],
  toolCalls: Record<string, unknown>[],
): { root: SpanNode | null; totalMs: number; agentColors: Map<string, string> } {
  if (spans.length === 0) return { root: null, totalMs: 0, agentColors: new Map() };

  // Build agent map from spans (uses nova.agent.id attribute or nearest ancestor name)
  const spanAgentMap = buildSpanAgentMap(spans);

  // Normalise model/tool calls for timestamp lookup
  const normModels = modelCalls.map(normalizeModelCall);
  const normTools = toolCalls.map(normalizeToolCall);
  propagateAgents(normModels, normTools);

  // Collect all agent names for palette assignment
  const allAgents = [
    ...normModels.map(m => m.agent).filter(Boolean),
    ...normTools.map(t => t.agent).filter(Boolean),
    ...[...spanAgentMap.values()],
  ] as string[];
  const agentColors = buildAgentColors(allAgents);

  // Build tsMap: span_id → ms-from-root using parent_span_id matching
  const allTs = [
    ...normModels.map(m => m.startedMs),
    ...normTools.map(t => t.startedMs),
  ].filter(t => t > 0);
  const t0 = allTs.length > 0 ? Math.min(...allTs) : 0;

  // Map from span_id to ms offset, using parent_span_id from calls
  const tsMap = new Map<string, number>();

  // Root always starts at 0
  const rootSpan = spans.find(s => !s.parent_span_id);
  if (rootSpan?.span_id) tsMap.set(rootSpan.span_id, 0);

  // Model calls: set their parent span's offset from model call's started_at
  for (const mc of normModels) {
    if (mc.parentSpanId && mc.startedMs > 0) {
      const off = Math.max(0, mc.startedMs - t0);
      if (!tsMap.has(mc.parentSpanId)) tsMap.set(mc.parentSpanId, off);
    }
  }
  // Tool calls same
  for (const tc of normTools) {
    if (tc.parentSpanId && tc.startedMs > 0) {
      const off = Math.max(0, tc.startedMs - t0);
      if (!tsMap.has(tc.parentSpanId)) tsMap.set(tc.parentSpanId, off);
    }
  }

  // For any span with started_at, use it directly
  for (const sp of spans) {
    if (sp.span_id && sp.started_at && t0 > 0) {
      const ts = parseTs(sp.started_at);
      if (ts > 0 && !tsMap.has(sp.span_id)) {
        tsMap.set(sp.span_id, Math.max(0, ts - t0));
      }
    }
  }

  // Build parent→children map + node map
  const childrenMap = new Map<string, SpanNode[]>();
  const nodeMap = new Map<string, SpanNode>();
  let root: SpanNode | null = null;

  const sorted = [...spans].sort((a, b) => {
    if (!a.parent_span_id) return -1;
    if (!b.parent_span_id) return 1;
    return (tsMap.get(a.span_id ?? '') ?? 0) - (tsMap.get(b.span_id ?? '') ?? 0);
  });

  for (const sp of sorted) {
    const id = sp.span_id ?? String(Math.random());
    const node: SpanNode = {
      id,
      name: sp.name ?? 'span',
      durationMs: sp.duration_ms ?? 0,
      kind: sp.kind ?? 'internal',
      startOffset: tsMap.get(id) ?? 0,
      status: sp.status,
      agent: spanAgentMap.get(id),
      children: [],
      depth: 0,
    };
    nodeMap.set(id, node);
    if (!sp.parent_span_id) {
      root = node;
    } else {
      if (!childrenMap.has(sp.parent_span_id)) childrenMap.set(sp.parent_span_id, []);
      childrenMap.get(sp.parent_span_id)!.push(node);
    }
  }

  function attach(node: SpanNode, depth: number) {
    node.depth = depth;
    node.children = childrenMap.get(node.id) ?? [];
    node.children.sort((a, b) => a.startOffset - b.startOffset);
    for (const c of node.children) attach(c, depth + 1);
  }
  if (root) attach(root, 0);

  // Fill missing offsets for orphaned children using sequential cursor
  function fillOffsets(node: SpanNode) {
    let cursor = node.startOffset;
    for (const c of node.children) {
      // Only fill if the offset is genuinely unknown (0 and cursor > 0)
      if (c.startOffset === 0 && cursor > 0) c.startOffset = cursor;
      cursor = Math.max(cursor, c.startOffset + c.durationMs);
      fillOffsets(c);
    }
  }
  if (root) fillOffsets(root);

  // Attach synthetic child nodes for model calls and tool calls.
  // All model/tool calls are direct children of the root capture span.
  if (root) {
    const syntheticChildren: SpanNode[] = [];

    for (const mc of normModels) {
      syntheticChildren.push({
        id: mc.id || `mc-${Math.random()}`,
        name: mc.model || 'model-call',
        durationMs: mc.durationMs,
        kind: 'model',
        startOffset: mc.startedMs > 0 ? Math.max(0, mc.startedMs - t0) : 0,
        status: mc.status === 'success' || mc.status === 'ok' ? 'ok' : 'error',
        agent: mc.agent,
        children: [],
        depth: 1,
      });
    }

    for (const tc of normTools) {
      syntheticChildren.push({
        id: tc.id || `tc-${Math.random()}`,
        name: tc.toolName,
        durationMs: tc.durationMs,
        kind: 'tool',
        startOffset: tc.startedMs > 0 ? Math.max(0, tc.startedMs - t0) : 0,
        status: tc.status === 'success' || tc.status === 'ok' ? 'ok' : 'error',
        agent: tc.agent,
        children: [],
        depth: 1,
      });
    }

    // Sort all synthetic children by start offset, then append to root
    syntheticChildren.sort((a, b) => a.startOffset - b.startOffset);
    // Merge with any existing span children (preserve span-tree children at depth 1+)
    root.children = [...root.children, ...syntheticChildren].sort(
      (a, b) => a.startOffset - b.startOffset,
    );
  }

  const totalMs = root?.durationMs ?? 0;
  return { root, totalMs, agentColors };
}

function SpanRow({
  node,
  rootDuration,
  collapsed,
  onToggle,
  agentColors,
}: {
  node: SpanNode;
  rootDuration: number;
  collapsed: Set<string>;
  onToggle: (id: string) => void;
  agentColors: Map<string, string>;
}) {
  const isCollapsed = collapsed.has(node.id);
  const leftPct = rootDuration > 0 ? (node.startOffset / rootDuration) * 100 : 0;
  const widthPct = rootDuration > 0 ? Math.max((node.durationMs / rootDuration) * 100, 0.5) : 2;
  const hasChildren = node.children.length > 0;
  const ok = spanStatusOk(node.status);

  // Bar colour: model calls use accent purple; tool calls use teal; spans use outcome colour
  const barColor = node.kind === 'model'
    ? (ok ? 'bg-purple-500' : 'bg-[var(--color-status-failure)]')
    : node.kind === 'tool'
    ? (ok ? 'bg-teal-500' : 'bg-[var(--color-status-failure)]')
    : (ok ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-failure)]');

  // Name prefix icon for synthetic rows
  const kindPrefix = node.kind === 'model' ? '◈ ' : node.kind === 'tool' ? '⚙ ' : '';

  // Agent accent for the label column dot
  const agentColor = node.agent ? (agentColors.get(node.agent) ?? '#6b7280') : '#6b7280';

  const durationLabel = node.durationMs >= 1000
    ? `${(node.durationMs / 1000).toFixed(2)}s`
    : `${node.durationMs}ms`;

  return (
    <>
      <div className="flex items-center group hover:bg-[var(--color-bg-sunken)] transition-colors min-h-[28px]">
        {/* Label column */}
        <div
          className="shrink-0 flex items-center gap-1 pr-2 truncate"
          style={{ width: '240px', paddingLeft: `${node.depth * 14 + 8}px` }}
        >
          {hasChildren ? (
            <button
              onClick={() => onToggle(node.id)}
              className="w-3 h-3 flex items-center justify-center text-[var(--color-text-faint)] hover:text-[var(--color-text)] shrink-0 text-[8px]"
              aria-label={isCollapsed ? 'expand' : 'collapse'}
            >
              {isCollapsed ? '▶' : '▼'}
            </button>
          ) : (
            <span className="w-3 shrink-0" />
          )}
          {/* Outcome dot */}
          <span
            className={clsx('w-1.5 h-1.5 rounded-full shrink-0', ok ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-failure)]')}
          />
          <span
            className={clsx(
              'text-xs font-mono truncate',
              node.depth === 0 ? 'font-medium text-[var(--color-text)]' : 'text-[var(--color-text-muted)]',
            )}
            title={node.name}
          >
            {kindPrefix}{node.name}
          </span>
        </div>

        {/* Agent label column */}
        <div className="shrink-0 w-24 px-1 truncate">
          {node.agent && node.depth > 0 && (
            <span
              className="text-[9px] font-mono truncate"
              style={{ color: agentColor }}
              title={node.agent}
            >
              {node.agent}
            </span>
          )}
        </div>

        {/* Timeline bar */}
        <div className="flex-1 relative h-7 flex items-center">
          <div
            className={clsx('absolute h-3.5 rounded-sm opacity-75 group-hover:opacity-100 transition-opacity', barColor)}
            style={{ left: `${leftPct}%`, width: `${widthPct}%`, minWidth: '3px' }}
            title={`${node.name}: ${durationLabel} (offset ${node.startOffset}ms)${node.agent ? ` [${node.agent}]` : ''}`}
          />
        </div>

        {/* Duration */}
        <div className="shrink-0 text-[10px] font-mono text-[var(--color-text-faint)] tabular-nums w-16 text-right pr-2">
          {durationLabel}
        </div>
      </div>

      {!isCollapsed && node.children.map(c => (
        <SpanRow
          key={c.id}
          node={c}
          rootDuration={rootDuration}
          collapsed={collapsed}
          onToggle={onToggle}
          agentColors={agentColors}
        />
      ))}
    </>
  );
}

interface TraceWaterfallProps {
  trace: unknown[];
  modelCalls: unknown[];
  toolCalls: unknown[];
}

export default function TraceWaterfall({ trace, modelCalls, toolCalls }: TraceWaterfallProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const toggle = (id: string) => setCollapsed(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const { root, totalMs, agentColors } = useMemo(
    () => buildTree(
      trace as RawSpan[],
      modelCalls as Record<string, unknown>[],
      toolCalls as Record<string, unknown>[],
    ),
    [trace, modelCalls, toolCalls],
  );

  if (!root) {
    return (
      <div className="p-8 text-center">
        <p className="text-sm text-[var(--color-text-muted)]">No trace spans recorded in this capsule.</p>
      </div>
    );
  }

  const tickCount = 5;
  const ticks = Array.from({ length: tickCount + 1 }, (_, i) => ({
    pct: (i / tickCount) * 100,
    label: `${Math.round((totalMs * i) / tickCount)}ms`,
  }));

  // Unique agents with colours for legend
  const agentEntries = [...agentColors.entries()];

  return (
    <div>
      {/* Stats bar */}
      <div className="flex items-center gap-4 px-2 py-2 mb-2 text-[10px] font-mono text-[var(--color-text-faint)]">
        <span>total: <strong className="text-[var(--color-text)]">{totalMs}ms</strong></span>
        <span>spans: {trace.length}</span>
        <span>models: {modelCalls.length}</span>
        <span>tools: {toolCalls.length}</span>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 px-2 mb-3 flex-wrap">
        <span className="flex items-center gap-1 text-[9px] text-[var(--color-text-faint)]">
          <span className="w-3 h-2 rounded-sm bg-[var(--color-status-success)] inline-block" /> span ok
        </span>
        <span className="flex items-center gap-1 text-[9px] text-[var(--color-text-faint)]">
          <span className="w-3 h-2 rounded-sm bg-[var(--color-status-failure)] inline-block" /> error
        </span>
        {modelCalls.length > 0 && (
          <span className="flex items-center gap-1 text-[9px] text-[var(--color-text-faint)]">
            <span className="w-3 h-2 rounded-sm bg-purple-500 inline-block" /> ◈ model call
          </span>
        )}
        {toolCalls.length > 0 && (
          <span className="flex items-center gap-1 text-[9px] text-[var(--color-text-faint)]">
            <span className="w-3 h-2 rounded-sm bg-teal-500 inline-block" /> ⚙ tool call
          </span>
        )}
        {agentEntries.map(([name, color]) => (
          <span key={name} className="flex items-center gap-1 text-[9px]" style={{ color }}>
            <span className="w-2 h-2 rounded-full inline-block" style={{ backgroundColor: color }} />
            {name}
          </span>
        ))}
      </div>

      {/* Timeline ruler */}
      <div className="flex border-b border-[var(--color-border)] mb-1 pb-1 text-[9px] font-mono text-[var(--color-text-faint)]">
        <div className="shrink-0" style={{ width: '240px' }} />
        <div className="shrink-0 w-24" />
        <div className="flex-1 relative">
          {ticks.map(t => (
            <span
              key={t.pct}
              className="absolute transform -translate-x-1/2"
              style={{ left: `${t.pct}%` }}
            >
              {t.label}
            </span>
          ))}
        </div>
        <div className="shrink-0 w-16" />
      </div>

      {/* Column headers */}
      <div className="flex items-center text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-2 py-1 border-b border-[var(--color-border)]">
        <div className="shrink-0" style={{ width: '240px' }}>span</div>
        <div className="shrink-0 w-24">agent</div>
        <div className="flex-1">timeline</div>
        <div className="shrink-0 w-16 text-right">dur</div>
      </div>

      {/* Span rows */}
      <div className="divide-y divide-[var(--color-border)] rounded border border-[var(--color-border)] overflow-hidden mt-1">
        <SpanRow
          node={root}
          rootDuration={totalMs}
          collapsed={collapsed}
          onToggle={toggle}
          agentColors={agentColors}
        />
      </div>
    </div>
  );
}
