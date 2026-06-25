/**
 * AgentSwimlane — Gantt-style view with one horizontal lane per agent.
 *
 * Each model call and tool call is rendered as a bar in its agent's lane.
 * The x-axis is wall-clock time (ms from first event).
 * Bar colour encodes outcome: green = success, red = error/failure.
 *
 * Degrades gracefully: when no agent attribution is present all calls appear
 * in a single "root" lane.
 */
import { useMemo } from 'react';
import { clsx } from 'clsx';
import {
  normalizeModelCall,
  normalizeToolCall,
  propagateAgents,
  buildAgentColors,
  agentInitial,
  type NormalizedModelCall,
  type NormalizedToolCall,
} from '../../lib/traceUtils';

// ─── Types ────────────────────────────────────────────────────────────────────

type CallBar =
  | { kind: 'model'; data: NormalizedModelCall }
  | { kind: 'tool';  data: NormalizedToolCall };

interface Lane {
  agent: string;
  color: string;
  bars: CallBar[];
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const ROOT_LANE = 'root';
const FALLBACK_COLOR = '#6b7280';
const MIN_BAR_PX = 4;

function callOk(bar: CallBar): boolean {
  const status = bar.kind === 'model' ? bar.data.status : bar.data.status;
  const resultErr = bar.kind === 'tool' ? bar.data.resultError : false;
  return (status === 'success' || status === 'ok') && !resultErr;
}

function callLabel(bar: CallBar): string {
  if (bar.kind === 'model') return bar.data.model || 'LLM';
  return bar.data.toolName || 'tool';
}

// ─── Tooltip ──────────────────────────────────────────────────────────────────

function barTitle(bar: CallBar): string {
  if (bar.kind === 'model') {
    const m = bar.data;
    const tokens = m.inputTokens != null
      ? ` | ${m.inputTokens}↑ ${m.outputTokens ?? '?'}↓ tok`
      : '';
    return `[${m.agent ?? 'root'}] ${m.model} — ${m.durationMs}ms${tokens} — ${m.status}`;
  }
  const t = bar.data;
  return `[${t.agent ?? 'root'}] ${t.toolName} — ${t.durationMs}ms — ${t.status}`;
}

// ─── Build lanes ─────────────────────────────────────────────────────────────

function buildLanes(
  modelCalls: Record<string, unknown>[],
  toolCalls: Record<string, unknown>[],
): { lanes: Lane[]; totalMs: number; t0: number } {
  const normModels = modelCalls.map(normalizeModelCall);
  const normTools  = toolCalls.map(normalizeToolCall);
  propagateAgents(normModels, normTools);

  const allAgents = [
    ...normModels.map(m => m.agent ?? ROOT_LANE),
    ...normTools.map(t => t.agent ?? ROOT_LANE),
  ];
  const agentColors = buildAgentColors(allAgents);

  const allTs = [
    ...normModels.map(m => m.startedMs),
    ...normTools.map(t => t.startedMs),
  ].filter(t => t > 0);

  if (allTs.length === 0) {
    return { lanes: [], totalMs: 0, t0: 0 };
  }

  const t0 = Math.min(...allTs);
  let maxEnd = 0;

  // Assign bars to lanes
  const laneMap = new Map<string, CallBar[]>();

  for (const m of normModels) {
    const key = m.agent ?? ROOT_LANE;
    if (!laneMap.has(key)) laneMap.set(key, []);
    laneMap.get(key)!.push({ kind: 'model', data: m });
    maxEnd = Math.max(maxEnd, m.startedMs - t0 + m.durationMs);
  }
  for (const t of normTools) {
    const key = t.agent ?? ROOT_LANE;
    if (!laneMap.has(key)) laneMap.set(key, []);
    laneMap.get(key)!.push({ kind: 'tool', data: t });
    maxEnd = Math.max(maxEnd, t.startedMs - t0 + t.durationMs);
  }

  const totalMs = maxEnd > 0 ? maxEnd : 1;

  // Sort bars in each lane by start time
  const lanes: Lane[] = [...laneMap.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([agent, bars]) => ({
      agent,
      color: agentColors.get(agent) ?? FALLBACK_COLOR,
      bars: bars.sort((a, b) => {
        const ta = a.kind === 'model' ? a.data.startedMs : a.data.startedMs;
        const tb = b.kind === 'model' ? b.data.startedMs : b.data.startedMs;
        return ta - tb;
      }),
    }));

  return { lanes, totalMs, t0 };
}

// ─── Components ───────────────────────────────────────────────────────────────

const LANE_HEIGHT = 36; // px
const LABEL_WIDTH = 160; // px

function Bar({
  bar,
  t0,
  totalMs,
  containerWidth,
}: {
  bar: CallBar;
  t0: number;
  totalMs: number;
  containerWidth: number;
}) {
  const startedMs = bar.kind === 'model' ? bar.data.startedMs : bar.data.startedMs;
  const durationMs = bar.kind === 'model' ? bar.data.durationMs : bar.data.durationMs;
  const offset = startedMs > 0 ? startedMs - t0 : 0;
  const ok = callOk(bar);

  const leftPct = totalMs > 0 ? (offset / totalMs) * 100 : 0;
  const widthPct = totalMs > 0 ? Math.max((durationMs / totalMs) * 100, 0) : 0;
  const minWidthPct = (MIN_BAR_PX / containerWidth) * 100;
  const finalWidthPct = Math.max(widthPct, minWidthPct);

  const label = callLabel(bar);

  return (
    <div
      className={clsx(
        'absolute top-1/2 -translate-y-1/2 h-5 rounded flex items-center px-1 overflow-hidden cursor-default transition-opacity hover:opacity-100',
        ok
          ? 'bg-[var(--color-status-success)] opacity-75'
          : 'bg-[var(--color-status-failure)] opacity-75',
      )}
      style={{ left: `${leftPct}%`, width: `${finalWidthPct}%`, minWidth: `${MIN_BAR_PX}px` }}
      title={barTitle(bar)}
    >
      <span className="text-[9px] font-mono text-white truncate leading-none">{label}</span>
    </div>
  );
}

function SwimlaneRow({
  lane,
  t0,
  totalMs,
  containerWidth,
}: {
  lane: Lane;
  t0: number;
  totalMs: number;
  containerWidth: number;
}) {
  return (
    <div className="flex items-stretch border-b border-[var(--color-border)] last:border-b-0">
      {/* Agent label */}
      <div
        className="shrink-0 flex items-center gap-1.5 px-2 border-r border-[var(--color-border)]"
        style={{ width: `${LABEL_WIDTH}px`, minHeight: `${LANE_HEIGHT}px` }}
      >
        <div
          className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold shrink-0"
          style={{ background: `${lane.color}25`, color: lane.color }}
        >
          {agentInitial(lane.agent)}
        </div>
        <span
          className="text-[10px] font-mono truncate"
          style={{ color: lane.color }}
          title={lane.agent}
        >
          {lane.agent}
        </span>
        <span className="ml-auto text-[9px] text-[var(--color-text-faint)] shrink-0">
          {lane.bars.length}
        </span>
      </div>

      {/* Timeline track */}
      <div className="flex-1 relative" style={{ minHeight: `${LANE_HEIGHT}px` }}>
        {lane.bars.map((bar, i) => (
          <Bar
            key={i}
            bar={bar}
            t0={t0}
            totalMs={totalMs}
            containerWidth={containerWidth}
          />
        ))}
      </div>
    </div>
  );
}

// ─── Ruler ────────────────────────────────────────────────────────────────────

function Ruler({ totalMs, labelWidth }: { totalMs: number; labelWidth: number }) {
  const tickCount = 6;
  const ticks = Array.from({ length: tickCount + 1 }, (_, i) => ({
    pct: (i / tickCount) * 100,
    label: totalMs >= 1000
      ? `${((totalMs * i) / tickCount / 1000).toFixed(1)}s`
      : `${Math.round((totalMs * i) / tickCount)}ms`,
  }));

  return (
    <div className="flex border-b border-[var(--color-border)] text-[9px] font-mono text-[var(--color-text-faint)]">
      <div className="shrink-0 border-r border-[var(--color-border)]" style={{ width: `${labelWidth}px`, height: '20px' }} />
      <div className="flex-1 relative" style={{ height: '20px' }}>
        {ticks.map(t => (
          <span
            key={t.pct}
            className="absolute top-1 transform -translate-x-1/2"
            style={{ left: `${t.pct}%` }}
          >
            {t.label}
          </span>
        ))}
      </div>
    </div>
  );
}

// ─── Main export ──────────────────────────────────────────────────────────────

interface AgentSwimlaneProps {
  modelCalls: unknown[];
  toolCalls: unknown[];
}

export default function AgentSwimlane({ modelCalls, toolCalls }: AgentSwimlaneProps) {
  const { lanes, totalMs, t0 } = useMemo(
    () => buildLanes(
      modelCalls as Record<string, unknown>[],
      toolCalls as Record<string, unknown>[],
    ),
    [modelCalls, toolCalls],
  );

  if (lanes.length === 0) {
    return (
      <div className="p-8 text-center">
        <p className="text-sm text-[var(--color-text-muted)]">
          No model or tool calls with timestamps recorded in this capsule.
        </p>
      </div>
    );
  }

  // Estimate container width for minimum bar width calculation.
  // We use a fixed estimate; CSS handles the actual rendering.
  const estimatedContainerWidth = 800;

  const hasMixedAgents = lanes.length > 1 || !lanes.some(l => l.agent === ROOT_LANE);

  return (
    <div>
      {/* Stats */}
      <div className="flex items-center gap-4 px-2 py-2 mb-2 text-[10px] font-mono text-[var(--color-text-faint)]">
        <span>agents: <strong className="text-[var(--color-text)]">{lanes.length}</strong></span>
        <span>models: {modelCalls.length}</span>
        <span>tools: {toolCalls.length}</span>
        <span>wall-clock: <strong className="text-[var(--color-text)]">
          {totalMs >= 1000 ? `${(totalMs / 1000).toFixed(2)}s` : `${totalMs}ms`}
        </strong></span>
      </div>

      {!hasMixedAgents && (
        <p className="text-[10px] text-[var(--color-text-faint)] px-2 mb-2 italic">
          No agent attribution found — add <code className="font-mono">nova.agent.id</code> to model/tool call records or emit agent system prompts for automatic inference.
        </p>
      )}

      {/* Legend */}
      <div className="flex items-center gap-4 px-2 mb-2">
        <span className="flex items-center gap-1 text-[9px] text-[var(--color-text-faint)]">
          <span className="w-3 h-2 rounded bg-[var(--color-status-success)] inline-block" /> success
        </span>
        <span className="flex items-center gap-1 text-[9px] text-[var(--color-text-faint)]">
          <span className="w-3 h-2 rounded bg-[var(--color-status-failure)] inline-block" /> error
        </span>
        <span className="text-[9px] text-[var(--color-text-faint)]">hover bar for details</span>
      </div>

      {/* Ruler + lanes */}
      <div className="rounded border border-[var(--color-border)] overflow-hidden">
        <Ruler totalMs={totalMs} labelWidth={LABEL_WIDTH} />
        {lanes.map(lane => (
          <SwimlaneRow
            key={lane.agent}
            lane={lane}
            t0={t0}
            totalMs={totalMs}
            containerWidth={estimatedContainerWidth}
          />
        ))}
      </div>
    </div>
  );
}
