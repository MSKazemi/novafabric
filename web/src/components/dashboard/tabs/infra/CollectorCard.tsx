// DD-2: Collector Status Card. Extracted verbatim from InfraTab.tsx
// (dashboard-modernization split).
import { useEffect, useState } from 'react';
import { api } from '../../../../lib/api';
import { usePolling } from '../../../../lib/usePolling';
import { BADGE_COLOR, StatRow } from './badges';

type CollectorData = Awaited<ReturnType<typeof api.collectorStatus>>;

export default function CollectorCard() {
  const [data, setData] = useState<CollectorData | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);

  const doFetch = () => {
    setLoading(true);
    api.collectorStatus()
      .then((d) => {
        setData(d);
        setLastChecked(new Date());
      })
      .catch(() => {/* silently ignore — endpoint may not be available yet */})
      .finally(() => setLoading(false));
  };

  // Initial load on mount; recurring poll pauses while the tab is hidden.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { doFetch(); }, []);
  usePolling(doFetch, 5000);

  const relativeTime = lastChecked
    ? (() => {
        const sec = Math.floor((Date.now() - lastChecked.getTime()) / 1000);
        if (sec < 5) return 'just now';
        if (sec < 60) return `${sec}s ago`;
        return `${Math.floor(sec / 60)}m ago`;
      })()
    : null;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">Collector — Cluster-Scale Event Ingestion</h3>
            <span className={`text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
              live
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">Phase 2 — v0.12</div>
        </div>
        <button
          onClick={doFetch}
          disabled={loading}
          className="shrink-0 text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50"
        >
          {loading ? 'Checking…' : 'Refresh'}
        </button>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Go-based high-throughput event collector for HPC/Kubernetes environments — NATS leaf node lifecycle, cap-001 rename-commit JSONL spool, NovaSeal batch signing, Prometheus metrics (ADR-0043).
      </p>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">Shipped</div>
        <ul className="space-y-0.5">
          {[
            'novafabric-collector binary (Go, github.com/novafabric/collector)',
            'novafabric-verifier — offline signature verifier',
            'novafabric-hpc-hub — NATS leaf lifecycle wrapper',
            'deploy/hpc/ — prolog.sh, epilog.sh, NATS templates, Ansible playbook',
            'deploy/k8s/ — DaemonSet (Fluent Bit), ConfigMap, Secret',
            '1000-event deterministic reference corpus validated',
            'Target: 100K events/sec at spool; NovaSeal p99 < 200ms',
          ].map((s, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[var(--color-text-muted)]">
              <span className="shrink-0 mt-px text-[var(--color-status-success)]">✓</span>
              {s}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2.5 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)]">Live Status</span>
          {relativeTime && (
            <span className="text-[10px] text-[var(--color-text-faint)]">Last checked: {relativeTime}</span>
          )}
        </div>

        {data === null ? (
          <p className="text-[11px] text-[var(--color-text-faint)]">Checking collector…</p>
        ) : data.detected ? (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
            <StatRow label="Status" value={<span className="text-[var(--color-status-success)] font-medium">Detected</span>} />
            <StatRow label="Version" value={data.collector_version ?? 'unknown'} />
            <StatRow label="Spool lag" value={data.spool_lag != null ? String(data.spool_lag) : 'N/A'} />
            <StatRow label="Signing p99" value={data.signing_p99_ms != null ? `${data.signing_p99_ms.toFixed(1)} ms` : 'N/A'} />
            <StatRow label="Events/sec" value={data.events_per_sec != null ? data.events_per_sec.toLocaleString() : 'N/A'} />
            <StatRow label="Heartbeat" value={data.last_heartbeat ?? 'N/A'} />
            {data.source && (
              <div className="col-span-2">
                <StatRow label="Source" value={<code className="font-mono text-[10px]">{data.source}</code>} />
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-[var(--color-status-pending)]" />
              <span className="text-[11px] text-[var(--color-status-pending)] font-medium">Collector not detected</span>
            </div>
            <p className="text-[11px] text-[var(--color-text-faint)] leading-relaxed">
              Install the collector binary or deploy to your cluster:
            </p>
            <ul className="space-y-1 text-[11px] text-[var(--color-text-muted)]">
              <li>
                <code className="font-mono text-[10px] bg-[var(--color-bg-raised)] px-1 py-0.5 rounded border border-[var(--color-border)]">
                  go install github.com/novafabric/collector/cmd/novafabric-collector@latest
                </code>
              </li>
              <li>Or use cluster configs: <code className="font-mono text-[10px]">deploy/hpc/</code> or <code className="font-mono text-[10px]">deploy/k8s/</code></li>
              <li>
                Health file: <code className="font-mono text-[10px]">~/.novafabric/collector-health.json</code>{' '}
                or <code className="font-mono text-[10px]">/tmp/novafabric-collector-health.json</code>
              </li>
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
