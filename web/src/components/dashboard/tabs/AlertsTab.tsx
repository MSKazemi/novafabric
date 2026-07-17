import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, type AlertRow, type AlertsRecentResult, type AlertSeverity } from '../../../lib/api';
import TabShell from './TabShell';

// Operational alerts feed (ADR-0192). Reads /api/alerts/recent — the
// hash-chained audit log's alert.delivery entries merged with recent ops.*
// events. Read-only surface; alerting is configured server-side via
// NOVA_ALERTS_*. Status colors reuse the theme's reserved status tokens
// (never the categorical chart palette) and always ship with a text label.

const labelClass =
  'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

const SEVERITY_STYLE: Record<AlertSeverity, string> = {
  critical:
    'text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]',
  warning:
    'text-[var(--color-status-pending)] border-[color-mix(in_oklab,var(--color-status-pending)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]',
  info: 'text-[var(--color-text-muted)] border-[var(--color-border)] bg-[var(--color-bg-sunken)]',
};

const OUTCOME_LABEL: Record<string, string> = {
  emitted: 'emitted',
  delivered: 'delivered',
  failed: 'delivery failed',
  dropped: 'dropped',
  deduped: 'deduped',
  'no-endpoint': 'no endpoint',
};

function SeverityBadge({ severity }: { severity: AlertSeverity | null }) {
  const sev = severity ?? 'info';
  const glyph = sev === 'critical' ? '●' : sev === 'warning' ? '▲' : '○';
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-mono ${SEVERITY_STYLE[sev]}`}
    >
      <span aria-hidden>{glyph}</span>
      {sev}
    </span>
  );
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  const failed = outcome === 'failed' || outcome === 'dropped';
  const cls = failed
    ? 'text-[var(--color-status-failure)]'
    : outcome === 'delivered'
      ? 'text-[var(--color-status-success)]'
      : 'text-[var(--color-text-faint)]';
  return <span className={`font-mono text-[10px] ${cls}`}>{OUTCOME_LABEL[outcome] ?? outcome}</span>;
}

function StatTile({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-4 py-3">
      <div className={labelClass}>{label}</div>
      <div className={`mt-1 text-xl font-semibold font-mono ${tone ?? 'text-[var(--color-text)]'}`}>
        {value}
      </div>
    </div>
  );
}

function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function AlertRowView({ row }: { row: AlertRow }) {
  return (
    <tr className="border-b border-[var(--color-border)] last:border-b-0 align-top">
      <td className="px-3 py-2 whitespace-nowrap">
        <SeverityBadge severity={row.severity} />
      </td>
      <td className="px-3 py-2 font-mono text-[11px] text-[var(--color-text)]">{row.event_type}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-[var(--color-text-muted)] break-all">
        {row.subject}
      </td>
      <td className="px-3 py-2 whitespace-nowrap">
        <OutcomeBadge outcome={row.outcome} />
        {row.endpoint_id && (
          <span className="ml-1 text-[10px] font-mono text-[var(--color-text-faint)]">
            → {row.endpoint_id}
            {row.attempts > 1 ? ` (${row.attempts}×)` : ''}
          </span>
        )}
      </td>
      <td className="px-3 py-2 whitespace-nowrap text-[10px] font-mono text-[var(--color-text-faint)]" title={row.timestamp}>
        {relativeTime(row.timestamp)}
      </td>
    </tr>
  );
}

export default function AlertsTab() {
  const [data, setData] = useState<AlertsRecentResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auto, setAuto] = useState(true);

  const load = useCallback(async () => {
    try {
      setError(null);
      setData(await api.alertsRecent(100));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    if (!auto) return;
    const id = setInterval(() => void load(), 15_000);
    return () => clearInterval(id);
  }, [load, auto]);

  const counts = useMemo(() => {
    const rows = data?.alerts ?? [];
    return {
      total: rows.length,
      critical: rows.filter((r) => r.severity === 'critical').length,
      failed: rows.filter((r) => r.outcome === 'failed' || r.outcome === 'dropped').length,
    };
  }, [data]);

  return (
    <TabShell
      title="Alerts"
      subtitle="Operational alerts (quota, rate-limit, policy, drift, seal, backup) and their delivery outcomes"
      cli={['nova audit-log export', 'nova events emit']}
      help="Read-only feed from the hash-chained audit log + ops.* events. Configure outbound delivery server-side with NOVA_ALERTS_* (Slack/PagerDuty/email/webhook). Alerting is OFF by default (ADR-0192)."
      actions={
        <button
          onClick={() => setAuto((a) => !a)}
          className={`rounded border px-2 py-1 text-[10px] font-mono ${
            auto
              ? 'border-[var(--color-accent)] text-[var(--color-accent)]'
              : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-border-strong)]'
          }`}
        >
          {auto ? 'live' : 'paused'}
        </button>
      }
    >
      {error && <p className="text-[11px] font-mono text-[var(--color-status-failure)]">{error}</p>}
      {data && (
        <div className="space-y-4">
          {!data.alerting_configured && (
            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] px-3 py-2 text-[11px] font-mono text-[var(--color-text-muted)]">
              Outbound alerting is not configured — events are recorded locally but not delivered.
              Set a <code>NOVA_ALERTS_*</code> endpoint to route Slack/PagerDuty/email/webhook.
            </div>
          )}
          <div className="grid grid-cols-3 gap-3">
            <StatTile label="recent alerts" value={String(counts.total)} />
            <StatTile
              label="critical"
              value={String(counts.critical)}
              tone={counts.critical ? 'text-[var(--color-status-failure)]' : undefined}
            />
            <StatTile
              label="delivery failures"
              value={String(counts.failed)}
              tone={counts.failed ? 'text-[var(--color-status-failure)]' : undefined}
            />
          </div>
          {data.alerts.length === 0 ? (
            <p className="text-[11px] font-mono text-[var(--color-text-faint)] py-4">
              No operational alerts recorded. Alerts appear here when a quota breach, sustained
              rate-limiting, policy violation, drift detection, seal-verify failure, or backup failure
              fires (ADR-0192).
            </p>
          ) : (
            <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                    <th className="px-3 py-2 text-left font-medium">severity</th>
                    <th className="px-3 py-2 text-left font-medium">event</th>
                    <th className="px-3 py-2 text-left font-medium">subject</th>
                    <th className="px-3 py-2 text-left font-medium">delivery</th>
                    <th className="px-3 py-2 text-left font-medium">when</th>
                  </tr>
                </thead>
                <tbody>
                  {data.alerts.map((row) => (
                    <AlertRowView key={row.id} row={row} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </TabShell>
  );
}
