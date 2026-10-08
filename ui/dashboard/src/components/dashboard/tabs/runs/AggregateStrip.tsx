/**
 * Runs aggregate strip — ADR-0234 (experimental).
 *
 * A compact bar strip above the run list: one bar per day over the current
 * date window, one metric at a time (runs / failed / p95 duration). It sends
 * the Runs view state to `/api/analytics/summary`, and the **server** decides
 * whether the aggregate can be computed faithfully (D2). When it cannot — a
 * filter, status chip or search the aggregate cannot apply, an index that is
 * behind the disk, no index at all — the strip renders the refusal's reason
 * and remedy, and its metric toggles are disabled **with that reason attached**
 * (D2/D3), instead of drawing bars for a different population than the list.
 *
 * Click-to-filter and drag-select (D1 navigation) are **planned**.
 */
import { useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { api, type AnalyticsSummary } from '../../../../lib/api';
import AggregateRefusal from '../../AggregateRefusal';

type Metric = 'runs' | 'failed' | 'p95';

const METRICS: Array<{ id: Metric; label: string }> = [
  { id: 'runs', label: 'runs' },
  { id: 'failed', label: 'failed' },
  { id: 'p95', label: 'p95' },
];

function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

export default function AggregateStrip({
  since, until, filterText, statusFilter, search, refreshTick,
}: {
  since: string;
  until: string;
  filterText: string;
  statusFilter: string;
  search: string;
  refreshTick: number;
}) {
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>('runs');

  useEffect(() => {
    if (typeof api.analyticsSummary !== 'function') return;
    let cancelled = false;
    setError(null);
    api.analyticsSummary({
      since: since || undefined,
      until: until || undefined,
      f: filterText.trim() || undefined,
      status: statusFilter,
      q: search.trim() || undefined,
    })
      .then(d => { if (!cancelled) setData(d); })
      .catch(e => { if (!cancelled) setError((e as Error).message); });
    return () => { cancelled = true; };
  }, [since, until, filterText, statusFilter, search, refreshTick]);

  const verdict = data?.aggregate;
  const refused = !!verdict && !verdict.computable;
  const disabledReason = refused ? (verdict?.reason ?? 'aggregate not computable') : undefined;

  const values = (data?.buckets ?? []).map(b => (
    metric === 'runs' ? b.run_count
      : metric === 'failed' ? b.failed_count
      : b.duration_ms_p95
  ));
  const max = Math.max(1, ...values.map(v => v ?? 0));
  const notes = verdict?.notes ?? {};
  const smallSample = Array.isArray(notes.small_sample_buckets) ? (notes.small_sample_buckets as string[]) : [];

  return (
    <div
      data-testid="aggregate-strip"
      aria-label="Run aggregates"
      className="px-3 py-2 border-b border-[var(--color-border)] space-y-1 shrink-0"
    >
      <div className="flex items-center gap-1.5">
        <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Aggregate
        </span>
        <span role="group" aria-label="Aggregate metric" className="flex gap-1">
          {METRICS.map(m => (
            <button
              key={m.id}
              type="button"
              onClick={() => setMetric(m.id)}
              disabled={refused}
              title={disabledReason ?? `Show ${m.label} per day`}
              aria-pressed={metric === m.id}
              className={clsx(
                'px-1.5 py-px rounded border text-[10px] font-mono',
                metric === m.id && !refused
                  ? 'border-[var(--color-accent)] text-[var(--color-accent)]'
                  : 'border-[var(--color-border)] text-[var(--color-text-muted)]',
                'disabled:opacity-40 disabled:cursor-not-allowed',
              )}
            >{m.label}</button>
          ))}
        </span>
        {data?.totals && !refused && (
          <span data-testid="aggregate-totals" className="ml-auto text-[10px] font-mono text-[var(--color-text-muted)]">
            {data.totals.run_count} runs · {data.totals.failed_count} failed
          </span>
        )}
      </div>

      {error && (
        <p role="status" className="text-[10px] text-[var(--color-text-faint)]">
          Aggregates unavailable: {error}
        </p>
      )}

      {refused && verdict && <AggregateRefusal verdict={verdict} compact testId="aggregate-strip-refusal" />}

      {!refused && data && data.buckets.length > 0 && (
        <div className="flex items-end gap-px h-8" role="img" aria-label={`${metric} per day`}>
          {data.buckets.map((b, i) => {
            const v = values[i];
            const h = v == null ? 0 : Math.max(2, Math.round((v / max) * 32));
            const label = v == null
              ? `${b.bucket}: no duration measured`
              : `${b.bucket}: ${metric === 'p95' ? fmtMs(v) : v}${metric === 'p95' ? ` (n=${b.duration_samples ?? '?'})` : ''}`;
            return (
              <span
                key={b.bucket}
                title={label}
                className={clsx(
                  'flex-1 rounded-sm',
                  v == null ? 'border border-dashed border-[var(--color-border)] h-2'
                    : metric === 'failed' ? 'bg-[var(--color-status-failure)]' : 'bg-[var(--color-accent)]',
                  metric === 'p95' && smallSample.includes(b.bucket) && 'opacity-50',
                )}
                style={v == null ? undefined : { height: `${h}px` }}
              />
            );
          })}
        </div>
      )}

      {!refused && data && data.buckets.length === 0 && !error && (
        <p className="text-[10px] text-[var(--color-text-faint)]">No runs in this window.</p>
      )}

      {!refused && metric === 'p95' && smallSample.length > 0 && (
        <p className="text-[10px] text-[var(--color-text-faint)]">
          Faded days rest on fewer than {String(notes.min_percentile_samples ?? 20)} runs — the p95 is exact for what was measured, but thin.
        </p>
      )}
    </div>
  );
}
