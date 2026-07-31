import { useState, useCallback } from 'react';
import { api } from '../../../../lib/api';
import type { ToolPermissionEventRecord } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';

// ---------- Tool Permission Events panel ----------

function PermissionBadge({ decision }: { decision: ToolPermissionEventRecord['decision'] }) {
  const cls =
    decision === 'allowed'
      ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
      : decision === 'denied'
      ? 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]'
      : 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]';
  return (
    <span className={`text-[9px] uppercase tracking-wider px-1 py-px rounded font-semibold ${cls}`}>
      {decision}
    </span>
  );
}

export default function ToolPermissionPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<ToolPermissionEventRecord[] | null>(null);

  const load = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setEvents(null);
    try {
      const res = await api.getToolPermissionEvents(id);
      setEvents(res.events);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId]);

  const denied = events?.filter(e => e.decision === 'denied').length ?? 0;
  const escalated = events?.filter(e => e.decision === 'escalated_to_human').length ?? 0;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Tool Permission Events</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-004 · EU AI Act Art. 9 · <code className="font-mono">nova capture</code> records per-tool decisions
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-004</span>
      </div>

      <div className="flex gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={load}
          placeholder="capsule run_id"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <button
          onClick={load}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? '…' : 'Load'}
        </button>
      </div>

      <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
        $ nova capture --run-id {runId || '<run_id>'} &amp;&amp; nova lineage provenance {runId || '<run_id>'}
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {events !== null && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 text-[10px] text-[var(--color-text-faint)]">
            <span>{events.length} event{events.length !== 1 ? 's' : ''}</span>
            {denied > 0 && <span className="text-[var(--color-status-failure)]">{denied} denied</span>}
            {escalated > 0 && <span className="text-[var(--color-status-pending)]">{escalated} escalated</span>}
          </div>
          {events.length === 0 ? (
            <p className="text-xs text-[var(--color-text-faint)] italic py-3 text-center">
              No tool permission events recorded for this capsule.
            </p>
          ) : (
            <div className="max-h-72 overflow-y-auto space-y-1 pr-1">
              {events.map((ev) => (
                <div
                  key={ev.event_id}
                  className="flex items-start gap-2 text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5"
                >
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="font-mono font-medium text-[var(--color-text)]">{ev.tool_name}</span>
                      <PermissionBadge decision={ev.decision} />
                      <span className="text-[var(--color-text-faint)]">{ev.permission_level}</span>
                    </div>
                    <div className="text-[var(--color-text-faint)] flex gap-3">
                      <span>policy: <span className="font-mono text-[var(--color-text-muted)]">{ev.policy_id}</span></span>
                      <span>latency: {ev.decision_latency_ms}ms</span>
                      {ev.human_approval_required && (
                        <span className={ev.human_approval_obtained ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-pending)]'}>
                          human-approval: {ev.human_approval_obtained ? '✓' : 'pending'}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
