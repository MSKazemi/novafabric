import { useState, useMemo } from 'react';
import { clsx } from 'clsx';
import TraceWaterfall from './TraceWaterfall';
import ConversationThread from './ConversationThread';
import AgentSwimlane from './AgentSwimlane';

type TraceTab = 'waterfall' | 'thread' | 'swimlane';

interface ToolStat {
  toolName: string;
  count: number;
  avgMs: number;
}

function ToolAnalyticsStrip({ toolCalls }: { toolCalls: unknown[] }) {
  const stats = useMemo<ToolStat[]>(() => {
    const groups = new Map<string, { total: number; count: number }>();
    for (const raw of toolCalls) {
      const rec = raw as Record<string, unknown>;
      const name = (rec['tool_name'] as string) ?? 'unknown';
      const dur = (rec['duration_ms'] as number) ?? 0;
      const existing = groups.get(name);
      if (existing) {
        existing.total += dur;
        existing.count += 1;
      } else {
        groups.set(name, { total: dur, count: 1 });
      }
    }
    return [...groups.entries()]
      .map(([toolName, { total, count }]) => ({
        toolName,
        count,
        avgMs: Math.round(total / count),
      }))
      .sort((a, b) => b.count - a.count);
  }, [toolCalls]);

  if (stats.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5 px-3 py-2 border-t border-[var(--color-border)]">
      {stats.map(s => (
        <span
          key={s.toolName}
          className="bg-[var(--color-bg-sunken)] border border-[var(--color-border)] rounded px-2 py-1 text-[10px] font-mono text-[var(--color-text-muted)]"
          title={`${s.toolName}: ${s.count} call${s.count !== 1 ? 's' : ''}, avg ${s.avgMs}ms`}
        >
          {s.toolName} ({s.count}× · avg {s.avgMs}ms)
        </span>
      ))}
    </div>
  );
}

interface TraceViewProps {
  trace: unknown[];
  modelCalls: unknown[];
  toolCalls: unknown[];
}

export default function TraceView({ trace, modelCalls, toolCalls }: TraceViewProps) {
  const [activeTab, setActiveTab] = useState<TraceTab>('waterfall');

  const tabs: { id: TraceTab; label: string; desc: string }[] = [
    { id: 'waterfall', label: 'Waterfall', desc: 'Span tree + Gantt timeline (outcome coloring, agent labels)' },
    { id: 'thread',    label: 'Thread',    desc: 'Chronological model + tool call view with agent badges and expandable detail' },
    { id: 'swimlane',  label: 'Swimlane',  desc: 'One horizontal lane per agent — shows parallel execution' },
  ];

  return (
    <div>
      {/* Sub-tab bar */}
      <div className="flex items-center gap-0.5 mb-4 border-b border-[var(--color-border)] pb-0 -mb-px">
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setActiveTab(t.id)}
            title={t.desc}
            className={clsx(
              'px-4 py-2 text-xs font-medium border-b-2 transition-colors',
              activeTab === t.id
                ? 'border-[var(--color-accent)] text-[var(--color-text)]'
                : 'border-transparent text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="pt-2">
        {activeTab === 'waterfall' && (
          <TraceWaterfall trace={trace} modelCalls={modelCalls} toolCalls={toolCalls} />
        )}
        {activeTab === 'thread' && (
          <ConversationThread modelCalls={modelCalls} toolCalls={toolCalls} />
        )}
        {activeTab === 'swimlane' && (
          <AgentSwimlane modelCalls={modelCalls} toolCalls={toolCalls} />
        )}
      </div>

      {toolCalls.length > 0 && <ToolAnalyticsStrip toolCalls={toolCalls} />}
    </div>
  );
}
