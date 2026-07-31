import { useMemo, useState } from 'react';
import { api, type AlertRow, type AlertSeverity } from '../../../lib/api';
import TabShell from './TabShell';
import DataTable, { type Column } from '../../ui/DataTable';
import TruncationNotice from '../../ui/TruncationNotice';
import Badge from '../../ui/primitives/Badge';
import Button from '../../ui/primitives/Button';
import { useQuery } from '../../../lib/useQuery';
import { usePolling } from '../../../lib/usePolling';

// Operational alerts feed (ADR-0192). Reads /api/alerts/recent — the
// hash-chained audit log's alert.delivery entries merged with recent ops.*
// events. Read-only surface; alerting is configured server-side via
// NOVA_ALERTS_*. Status colors reuse the theme's reserved status tokens
// (never the categorical chart palette) and always ship with a text label.

const labelClass =
  'text-2xs font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

const ALERTS_LIMIT = 100;

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
  const tone = sev === 'critical' ? 'danger' : sev === 'warning' ? 'pending' : 'neutral';
  return <Badge tone={tone} dot>{sev}</Badge>;
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  const failed = outcome === 'failed' || outcome === 'dropped';
  const cls = failed
    ? 'text-[var(--color-status-failure)]'
    : outcome === 'delivered'
      ? 'text-[var(--color-status-success)]'
      : 'text-[var(--color-text-faint)]';
  return <span className={`font-mono text-2xs ${cls}`}>{OUTCOME_LABEL[outcome] ?? outcome}</span>;
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

const COLUMNS: Column<AlertRow>[] = [
  {
    key: 'severity',
    header: 'severity',
    className: 'w-24',
    render: (row) => <SeverityBadge severity={row.severity} />,
    sortValue: (row) => row.severity ?? 'info',
  },
  {
    key: 'event_type',
    header: 'event',
    className: 'w-44',
    render: (row) => <span className="font-mono text-[11px]">{row.event_type}</span>,
    sortValue: (row) => row.event_type,
  },
  {
    key: 'subject',
    header: 'subject',
    render: (row) => (
      <span className="font-mono text-[11px] text-[var(--color-text-muted)] truncate">{row.subject}</span>
    ),
  },
  {
    key: 'delivery',
    header: 'delivery',
    className: 'w-48',
    render: (row) => (
      <span className="truncate">
        <OutcomeBadge outcome={row.outcome} />
        {row.endpoint_id && (
          <span className="ml-1 text-2xs font-mono text-[var(--color-text-faint)]">
            → {row.endpoint_id}
            {row.attempts > 1 ? ` (${row.attempts}×)` : ''}
          </span>
        )}
      </span>
    ),
  },
  {
    key: 'when',
    header: 'when',
    className: 'w-20',
    render: (row) => (
      <span className="text-2xs font-mono text-[var(--color-text-faint)]" title={row.timestamp}>
        {relativeTime(row.timestamp)}
      </span>
    ),
    sortValue: (row) => row.timestamp,
  },
];

export default function AlertsTab() {
  const [auto, setAuto] = useState(true);
  const [tick, setTick] = useState(0);

  const query = useQuery(() => api.alertsRecent(ALERTS_LIMIT), [tick]);
  usePolling(() => setTick((t) => t + 1), 15_000, auto);
  const data = query.data;

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
      icon="alerts"
      subtitle="Operational alerts (quota, rate-limit, policy, drift, seal, backup) and their delivery outcomes"
      cli={['nova audit-log export', 'nova events emit']}
      help="Read-only feed from the hash-chained audit log + ops.* events. Configure outbound delivery server-side with NOVA_ALERTS_* (Slack/PagerDuty/email/webhook). Alerting is OFF by default (ADR-0192)."
      actions={
        <Button size="sm" variant={auto ? 'secondary' : 'ghost'} onClick={() => setAuto((a) => !a)}>
          {auto ? 'live' : 'paused'}
        </Button>
      }
    >
      <div className="space-y-4">
        {data && !data.alerting_configured && (
          <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[var(--color-pending-tint)] px-3 py-2 text-[11px] font-mono text-[var(--color-text-muted)]">
            Outbound alerting is not configured — events are recorded locally but not delivered.
            Set a <code>NOVA_ALERTS_*</code> endpoint to route Slack/PagerDuty/email/webhook.
          </div>
        )}
        {data && (
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
        )}
        <DataTable
          columns={COLUMNS}
          rows={data?.alerts ?? []}
          rowKey={(row) => row.id}
          loading={query.loading && !data}
          error={query.error}
          onRetry={query.reload}
          rowHeight={36}
          empty={
            <p className="text-[11px] font-mono text-[var(--color-text-faint)] py-4">
              No operational alerts recorded. Alerts appear here when a quota breach, sustained
              rate-limiting, policy violation, drift detection, seal-verify failure, or backup failure
              fires (ADR-0192).
            </p>
          }
          footer={
            <TruncationNotice
              shown={data?.alerts.length ?? 0}
              hasMore={(data?.alerts.length ?? 0) >= ALERTS_LIMIT}
              hint="showing the most recent — export the audit log for full history"
            />
          }
        />
      </div>
    </TabShell>
  );
}
