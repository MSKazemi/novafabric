/**
 * Incidents — EU AI Act Art. 73 deadline clock (nova incident).
 *   open / list / status / export, with a live countdown to the nearest
 *   reporting obligation. Deadline outputs are operational aids, not legal
 *   advice (ADR-0088).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, type IncidentView } from '../../../lib/api';
import { useMutation } from '../../../lib/useMutation';
import TabShell from './TabShell';
import DataTable, { type Column } from '../../ui/DataTable';
import ActionButton from '../../ui/ActionButton';
import EmptyState from '../../ui/EmptyState';

const SEVERITIES = ['critical', 'high', 'medium', 'low'];
const NEXT_STATUS: Record<string, string | null> = { open: 'reported', reported: 'closed', closed: null };

/** Human countdown from now to an ISO deadline. */
function countdown(deadlineIso: string, nowMs: number): { text: string; overdue: boolean } {
  const secs = (new Date(deadlineIso).getTime() - nowMs) / 1000;
  const overdue = secs < 0;
  const a = Math.abs(secs);
  const d = Math.floor(a / 86400);
  const h = Math.floor((a % 86400) / 3600);
  const m = Math.floor((a % 3600) / 60);
  const text = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
  return { text: overdue ? `${text} overdue` : text, overdue };
}

function sevColor(sev: string): string {
  return sev === 'critical' || sev === 'high' ? 'var(--color-status-failure)'
    : sev === 'medium' ? 'var(--color-accent)' : 'var(--color-text-muted)';
}

export default function IncidentsTab({ onCountChange }: { onCountChange?: (n: number) => void }) {
  const [incidents, setIncidents] = useState<IncidentView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [showForm, setShowForm] = useState(false);

  // Form state
  const [title, setTitle] = useState('');
  const [classification, setClassification] = useState('');
  const [severity, setSeverity] = useState('high');
  const [occurredAt, setOccurredAt] = useState('');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api.incidentList()
      .then((r) => {
        const list = r.incidents ?? [];
        setIncidents(list);
        onCountChange?.(list.filter((i) => i.status !== 'closed').length);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [onCountChange]);
  useEffect(load, [load]);

  // Live clock: re-render the countdowns every 30s.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const open = useMutation(
    () => api.incidentOpen({
      title: title.trim(),
      classification: classification.trim(),
      severity,
      occurred_at: occurredAt || undefined,
    }),
    { successMessage: 'Incident opened', onSuccess: () => { setShowForm(false); setTitle(''); setClassification(''); load(); } },
  );
  const transition = useMutation(
    (id: string, to: string) => api.incidentTransition(id, to),
    { successMessage: 'Incident advanced', onSuccess: load },
  );
  const exportInc = useMutation(
    (id: string, fmt: 'aim' | 'nis2') => api.incidentExport(id, fmt),
    { silentSuccess: true },
  );

  const columns = useMemo<Column<IncidentView>[]>(() => [
    { key: 'title', header: 'Title', className: 'flex-1', sortValue: (r) => r.title,
      render: (r) => (
        <div className="min-w-0">
          <div className="truncate text-[var(--color-text)]">{r.title}</div>
          <div className="text-[10px] font-mono text-[var(--color-text-faint)] truncate">{r.classification}</div>
        </div>
      ) },
    { key: 'severity', header: 'Severity', className: 'w-20',
      render: (r) => <span className="text-[10px] uppercase font-mono" style={{ color: sevColor(r.severity) }}>{r.severity}</span> },
    { key: 'status', header: 'Status', className: 'w-20', sortValue: (r) => r.status,
      render: (r) => <span className="text-[10px] uppercase font-mono text-[var(--color-text-muted)]">{r.status}</span> },
    { key: 'deadline', header: 'Art.73 deadline', className: 'w-40', sortValue: (r) => r.nearest_deadline ?? '',
      render: (r) => {
        if (!r.nearest_deadline) return <span className="text-[10px] text-[var(--color-text-faint)]">—</span>;
        const obligation = r.nearest_obligation?.replace('art73_', 'Art.73 ');
        // The live urgency countdown only makes sense while reporting is still
        // pending (status=open). Once reported/closed the obligation is no
        // longer an action item — show a static "filed" marker, not a ticker.
        if (r.status !== 'open') {
          return (
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              ✓ filed<span className="ml-1">{obligation}</span>
            </span>
          );
        }
        const c = countdown(r.nearest_deadline, now);
        return (
          <span className="text-[10px] font-mono" style={{ color: c.overdue ? 'var(--color-status-failure)' : 'var(--color-text)' }}>
            {c.overdue ? '⚠ ' : '⏱ '}{c.text}
            <span className="text-[var(--color-text-faint)] ml-1">{obligation}</span>
          </span>
        );
      } },
  ], [now]);

  return (
    <TabShell
      title="Incidents"
      subtitle="EU AI Act Art. 73 serious-incident records with a live reporting-deadline clock."
      cli={['nova incident open', 'nova incident list', 'nova incident status', 'nova incident export']}
      help="The clock anchors to awareness (or occurrence). Deadlines: 15 days standard (Art.73(2)), 10 days for death (73(4)), 2 days for widespread/critical-infra (73(3)). Operational aid, not legal advice."
      actions={
        <>
          <ActionButton onClick={load} variant="ghost" size="sm">Refresh</ActionButton>
          <ActionButton onClick={() => setShowForm((v) => !v)} variant="primary" size="sm">{showForm ? 'Cancel' : 'Open incident'}</ActionButton>
        </>
      }
    >
      {showForm && (
        <section className="rounded border border-[var(--color-border)] p-4 space-y-2">
          <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">New incident</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
              Title
              <input value={title} onChange={(e) => setTitle(e.target.value)} className="px-2 py-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]" />
            </label>
            <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
              Classification
              <input value={classification} onChange={(e) => setClassification(e.target.value)} placeholder="e.g. critical_infrastructure_disruption" className="px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]" />
            </label>
            <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
              Severity
              <select value={severity} onChange={(e) => setSeverity(e.target.value)} className="px-2 py-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]">
                {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
              Occurred at (ISO, optional)
              <input value={occurredAt} onChange={(e) => setOccurredAt(e.target.value)} placeholder="2026-06-19T12:00:00Z" className="px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]" />
            </label>
          </div>
          <ActionButton onClick={() => open.run()} pending={open.pending} disabled={!title.trim() || !classification.trim()} variant="primary">Open incident</ActionButton>
        </section>
      )}

      <DataTable
        columns={columns}
        rows={incidents}
        rowKey={(r) => r.id}
        loading={loading}
        error={error}
        onRetry={load}
        empty={<EmptyState message="No incidents recorded." cliCommand="nova incident open --title …" />}
        rowActions={(r) => {
          const next = NEXT_STATUS[r.status];
          return (
            <>
              {next && (
                <ActionButton onClick={() => transition.run(r.id, next)} pending={transition.pending} size="sm">→ {next}</ActionButton>
              )}
              <ActionButton onClick={() => exportInc.run(r.id, 'aim')} pending={exportInc.pending} size="sm" variant="ghost">AIM</ActionButton>
              <ActionButton onClick={() => exportInc.run(r.id, 'nis2')} pending={exportInc.pending} size="sm" variant="ghost">NIS2</ActionButton>
            </>
          );
        }}
      />

      {exportInc.result?.report && (
        <section className="rounded border border-[var(--color-border)] p-3">
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Exported report ({exportInc.result.format})</div>
          <pre className="whitespace-pre-wrap font-mono text-[10px] text-[var(--color-text-muted)] max-h-72 overflow-auto">{JSON.stringify(exportInc.result.report, null, 2)}</pre>
        </section>
      )}
    </TabShell>
  );
}
