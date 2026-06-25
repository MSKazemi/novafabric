import { useState, useMemo } from 'react';
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

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${ms}ms`;
}

function outcomeOk(status: string): boolean {
  return status === 'success' || status === 'ok';
}

// ─── Agent avatar ─────────────────────────────────────────────────────────────

function AgentAvatar({
  agent,
  color,
  ok,
  letter,
}: {
  agent: string | undefined;
  color: string;
  ok: boolean;
  letter: string;
}) {
  return (
    <div
      className="shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold border-2 select-none"
      style={{
        background: `${color}22`,
        color,
        borderColor: ok ? color : 'var(--color-status-failure)',
      }}
      title={agent ?? 'unknown agent'}
    >
      {letter}
    </div>
  );
}

// ─── Expandable detail area ───────────────────────────────────────────────────

function ExpandSection({ label, content }: { label: string; content: string }) {
  return (
    <div className="mt-2">
      <p className="text-[9px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">{label}</p>
      <pre className="text-[10px] font-mono text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2 overflow-x-auto whitespace-pre-wrap break-all leading-relaxed">
        {content}
      </pre>
    </div>
  );
}

// ─── Model bubble ─────────────────────────────────────────────────────────────

function ModelBubble({
  data,
  color,
  expanded,
  onToggle,
}: {
  data: NormalizedModelCall;
  color: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const ok = outcomeOk(data.status);
  const letter = data.agent ? agentInitial(data.agent) : 'M';
  const hasDetail = !!(data.completionPreview || data.messages.length > 0);

  return (
    <div className="flex gap-3">
      <AgentAvatar agent={data.agent} color={color} ok={ok} letter={letter} />
      <div className="flex-1 min-w-0">
        {/* Header row */}
        <button
          className="w-full text-left"
          onClick={hasDetail ? onToggle : undefined}
          title={hasDetail ? (expanded ? 'Collapse' : 'Expand details') : undefined}
        >
          <div className="flex items-baseline gap-2 flex-wrap mb-0.5">
            {/* Agent badge */}
            {data.agent && (
              <span
                className="text-[9px] font-medium px-1 py-px rounded"
                style={{ background: `${color}20`, color }}
              >
                {data.agent}
              </span>
            )}
            <span className="text-xs font-medium text-[var(--color-text)]">{data.model}</span>
            {data.system && <span className="text-[10px] text-[var(--color-text-faint)]">{data.system}</span>}
            <span className="text-[10px] font-mono text-[var(--color-text-faint)] tabular-nums">
              {fmtDuration(data.durationMs)}
            </span>
            {/* Token counts */}
            {(data.inputTokens != null || data.outputTokens != null) && (
              <span className="text-[10px] font-mono text-[var(--color-text-faint)] tabular-nums">
                {data.inputTokens ?? '?'}↑ {data.outputTokens ?? '?'}↓ tok
              </span>
            )}
            {/* Cache indicator */}
            {data.cacheHit != null && (
              <span className={clsx(
                'text-[9px] px-1 py-px rounded',
                data.cacheHit
                  ? 'bg-[color-mix(in_oklab,#10b981_15%,transparent)] text-[#10b981]'
                  : 'bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)]',
              )}>
                {data.cacheHit ? 'cached' : 'no cache'}
              </span>
            )}
            {/* Finish reason */}
            {data.finishReason && (
              <span className={clsx(
                'text-[9px] uppercase tracking-wider px-1 py-px rounded font-medium',
                data.finishReason === 'stop'
                  ? 'bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)] text-[var(--color-status-success)]'
                  : data.finishReason === 'tool_calls'
                  ? 'bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] text-[var(--color-accent)]'
                  : 'bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] text-[var(--color-status-failure)]',
              )}>
                {data.finishReason}
              </span>
            )}
            {/* Status badge (when not success) */}
            {!ok && (
              <span className="text-[9px] uppercase tracking-wider px-1 py-px rounded bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] text-[var(--color-status-failure)] font-medium">
                {data.status}
              </span>
            )}
            {/* Expand chevron */}
            {hasDetail && (
              <span className="text-[10px] text-[var(--color-text-faint)] ml-auto">
                {expanded ? '▲' : '▼'}
              </span>
            )}
          </div>
          {/* Request metadata sub-line */}
          <p className="text-[10px] text-[var(--color-text-faint)]">
            {data.messages.length > 0 && (
              <span>{data.messages.length} msg{data.messages.length !== 1 ? 's' : ''}</span>
            )}
            {data.temperature != null && <span className="ml-2">temp {data.temperature}</span>}
            {data.maxTokens != null && <span className="ml-2">max {data.maxTokens} tok</span>}
            {data.phase && <span className="ml-2 italic">[{data.phase}]</span>}
          </p>
        </button>

        {/* Completion preview (always visible if present and short) */}
        {data.completionPreview && !expanded && (
          <div
            className="mt-1 rounded border border-[color-mix(in_oklab,var(--color-border)_80%,transparent)] bg-[var(--color-bg-sunken)] px-3 py-2 text-xs text-[var(--color-text)] font-sans leading-relaxed cursor-pointer"
            onClick={onToggle}
          >
            {data.completionPreview.length > 200
              ? data.completionPreview.slice(0, 200) + '…'
              : data.completionPreview}
          </div>
        )}

        {/* Expanded detail */}
        {expanded && (
          <div className="mt-2 space-y-2">
            {data.completionPreview && (
              <ExpandSection label="completion" content={data.completionPreview} />
            )}
            {data.messages.length > 0 && (() => {
              const last = data.messages[data.messages.length - 1];
              const content = typeof last.content === 'string'
                ? last.content.slice(0, 800)
                : JSON.stringify(last.content).slice(0, 800);
              return (
                <ExpandSection
                  label={`last message (${last.role})`}
                  content={content + (content.length >= 800 ? '…' : '')}
                />
              );
            })()}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Tool bubble ──────────────────────────────────────────────────────────────

function ToolBubble({
  data,
  color,
  expanded,
  onToggle,
}: {
  data: NormalizedToolCall;
  color: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const ok = outcomeOk(data.status) && !data.resultError;
  const letter = data.agent ? agentInitial(data.agent) : 'T';

  const argsStr = data.arguments
    ? JSON.stringify(data.arguments, null, 2)
    : null;

  return (
    <div className="flex gap-3">
      <AgentAvatar agent={data.agent} color={color} ok={ok} letter={letter} />
      <div className="flex-1 min-w-0">
        <button className="w-full text-left" onClick={onToggle}>
          <div className="flex items-baseline gap-2 flex-wrap mb-0.5">
            {/* Agent badge */}
            {data.agent && (
              <span
                className="text-[9px] font-medium px-1 py-px rounded"
                style={{ background: `${color}20`, color }}
              >
                {data.agent}
              </span>
            )}
            <code className="text-xs font-mono font-medium text-[var(--color-text)]">
              {data.toolName}
            </code>
            {data.transport && (
              <span className="text-[10px] text-[var(--color-text-faint)]">{data.transport}</span>
            )}
            <span className="text-[10px] font-mono text-[var(--color-text-faint)] tabular-nums">
              {fmtDuration(data.durationMs)}
            </span>
            {data.mutates && (
              <span className="text-[9px] uppercase tracking-wider px-1 py-px rounded bg-[color-mix(in_oklab,var(--color-status-pending)_15%,transparent)] text-[var(--color-status-pending)]">
                mutating
              </span>
            )}
            <span className={clsx(
              'text-[9px] uppercase tracking-wider px-1 py-px rounded font-medium',
              ok
                ? 'bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)] text-[var(--color-status-success)]'
                : 'bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] text-[var(--color-status-failure)]',
            )}>
              {data.status}
            </span>
            <span className="text-[10px] text-[var(--color-text-faint)] ml-auto">{expanded ? '▲' : '▼'}</span>
          </div>
          {/* Args preview (collapsed, one line) */}
          {!expanded && data.arguments && (
            <p className="text-[10px] font-mono text-[var(--color-text-faint)] truncate">
              {Object.entries(data.arguments)
                .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                .join(', ')}
            </p>
          )}
        </button>

        {/* Result preview (always visible when collapsed) */}
        {data.resultText && !expanded && (
          <div
            className="mt-1 rounded border border-[color-mix(in_oklab,var(--color-border)_80%,transparent)] bg-[var(--color-bg-sunken)] px-3 py-2 text-[10px] font-mono text-[var(--color-text-muted)] leading-relaxed cursor-pointer"
            onClick={onToggle}
          >
            {data.resultText.length > 200 ? data.resultText.slice(0, 200) + '…' : data.resultText}
          </div>
        )}

        {/* Expanded detail */}
        {expanded && (
          <div className="mt-2 space-y-2">
            {argsStr && <ExpandSection label="arguments" content={argsStr} />}
            {data.resultText && <ExpandSection label="result" content={data.resultText} />}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Agent group divider ──────────────────────────────────────────────────────

function AgentDivider({ agent, color }: { agent: string; color: string }) {
  return (
    <div className="flex items-center gap-2 py-1">
      <div className="flex-1 h-px bg-[var(--color-border)]" />
      <span
        className="text-[9px] font-mono uppercase tracking-widest px-2 py-0.5 rounded-full border"
        style={{ color, borderColor: `${color}40`, background: `${color}10` }}
      >
        {agent}
      </span>
      <div className="flex-1 h-px bg-[var(--color-border)]" />
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

interface ConversationThreadProps {
  modelCalls: unknown[];
  toolCalls: unknown[];
}

type EventItem =
  | { kind: 'model'; ts: number; data: NormalizedModelCall }
  | { kind: 'tool';  ts: number; data: NormalizedToolCall };

export default function ConversationThread({ modelCalls, toolCalls }: ConversationThreadProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleExpand = (id: string) => setExpanded(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const { events, agentColors } = useMemo(() => {
    const normModels = (modelCalls as Record<string, unknown>[]).map(normalizeModelCall);
    const normTools = (toolCalls as Record<string, unknown>[]).map(normalizeToolCall);
    propagateAgents(normModels, normTools);

    const allAgents = [
      ...normModels.map(m => m.agent),
      ...normTools.map(t => t.agent),
    ].filter(Boolean) as string[];
    const colors = buildAgentColors(allAgents);

    const evs: EventItem[] = [
      ...normModels.map(m => ({ kind: 'model' as const, ts: m.startedMs, data: m })),
      ...normTools.map(t => ({ kind: 'tool' as const, ts: t.startedMs, data: t })),
    ];
    evs.sort((a, b) => {
      if (a.ts !== b.ts) return a.ts - b.ts;
      // stable: models before tools when same timestamp
      if (a.kind !== b.kind) return a.kind === 'model' ? -1 : 1;
      return 0;
    });

    return { events: evs, agentColors: colors };
  }, [modelCalls, toolCalls]);

  if (events.length === 0) {
    return (
      <div className="p-8 text-center">
        <p className="text-sm text-[var(--color-text-muted)]">No model or tool calls recorded in this capsule.</p>
      </div>
    );
  }

  const UNKNOWN = 'unattributed';
  const UNKNOWN_COLOR = '#6b7280';

  let prevAgent: string | undefined = undefined;

  return (
    <div className="space-y-3 py-2">
      {events.map((ev, i) => {
        const id = ev.kind === 'model' ? ev.data.id : ev.data.id;
        const agent = (ev.kind === 'model' ? ev.data.agent : ev.data.agent) ?? UNKNOWN;
        const color = agentColors.get(agent) ?? UNKNOWN_COLOR;
        const isExpanded = expanded.has(id || String(i));

        const showDivider = i === 0 || agent !== prevAgent;
        const previousAgent = prevAgent;
        prevAgent = agent;

        return (
          <div key={id || i}>
            {/* Agent group divider when agent changes */}
            {showDivider && (
              <AgentDivider agent={agent} color={color} />
            )}

            {/* Timestamp */}
            {ev.ts > 0 && (
              <p className="text-[9px] font-mono text-[var(--color-text-faint)] mb-1.5 pl-10">
                {new Date(ev.ts).toISOString().slice(11, 23)}
              </p>
            )}

            {ev.kind === 'model' ? (
              <ModelBubble
                data={ev.data}
                color={color}
                expanded={isExpanded}
                onToggle={() => toggleExpand(id || String(i))}
              />
            ) : (
              <ToolBubble
                data={ev.data}
                color={color}
                expanded={isExpanded}
                onToggle={() => toggleExpand(id || String(i))}
              />
            )}

            {/* Connector line between events in same agent group */}
            {i < events.length - 1 && (
              <div className="ml-3.5 mt-2 w-px h-3 bg-[var(--color-border)]" aria-hidden="true" />
            )}
          </div>
        );
      })}
    </div>
  );
}
