import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, type AnalyticsBucket, type AnalyticsSummary, type QueryPanelResult } from '../../../lib/api';
import TabShell from './TabShell';
import ChartCard from '../../ui/ChartCard';
import DataTable, { type Column } from '../../ui/DataTable';

// Analytics tab — time-bucketed run metrics from /api/analytics/summary.
// Charts are plain SVG on the dashboard tokens; series colors are
// palette-validated (dataviz six-checks, dark surface #0a0a0c):
//   bars  completed #3987e5 / failed #d94f4f   (CVD ΔE 22.7)
//   lines p50 #3987e5 / p95 #c98500            (CVD ΔE 27.4)
const C_PRIMARY = '#3987e5';
const C_FAILED = '#d94f4f';
const C_P95 = '#c98500';

const RANGE_DAYS = [7, 30, 90] as const;
type RangeDays = (typeof RANGE_DAYS)[number];

const labelClass =
  'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

function isoDaysAgo(days: number): string {
  const d = new Date(Date.now() - days * 86_400_000);
  return d.toISOString().slice(0, 10);
}

function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—';
  if (v >= 60_000) return `${(v / 60_000).toFixed(1)}m`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}s`;
  return `${Math.round(v)}ms`;
}

function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-4 py-3">
      <div className={labelClass}>{label}</div>
      <div className="mt-1 text-xl font-semibold text-[var(--color-text)] font-mono">{value}</div>
      {sub && <div className="text-[10px] text-[var(--color-text-muted)] font-mono mt-0.5">{sub}</div>}
    </div>
  );
}

function LegendChip({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[10px] font-mono text-[var(--color-text-muted)]">
      <span className="inline-block h-2 w-2 rounded-sm" style={{ background: color }} />
      {label}
    </span>
  );
}

interface TooltipState {
  x: number;
  y: number;
  lines: string[];
}

function ChartTooltip({ tip }: { tip: TooltipState | null }) {
  if (!tip) return null;
  return (
    <div
      className="pointer-events-none absolute z-10 rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-sunken)] px-2 py-1 text-[10px] font-mono text-[var(--color-text)] shadow"
      style={{ left: tip.x + 10, top: tip.y - 8 }}
    >
      {tip.lines.map((l) => (
        <div key={l}>{l}</div>
      ))}
    </div>
  );
}

const W = 640;
const H = 180;
const PAD_L = 44;
const PAD_B = 22;
const PAD_T = 8;

/** Stacked run-volume bars: completed + failed per day, 2px gaps, rounded data-end. */
function RunVolumeBars({ buckets }: { buckets: AnalyticsBucket[] }) {
  const [tip, setTip] = useState<TooltipState | null>(null);
  const maxRuns = Math.max(1, ...buckets.map((b) => b.run_count));
  const innerW = W - PAD_L - 8;
  const innerH = H - PAD_T - PAD_B;
  const slot = innerW / Math.max(1, buckets.length);
  const barW = Math.max(3, Math.min(28, slot - 2)); // ≥2px gap between bars
  const yTicks = [0, Math.ceil(maxRuns / 2), maxRuns];

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Runs per day">
        {yTicks.map((t) => {
          const y = PAD_T + innerH * (1 - t / maxRuns);
          return (
            <g key={t}>
              <line x1={PAD_L} x2={W - 8} y1={y} y2={y} stroke="var(--color-border)" strokeWidth={1} />
              <text x={PAD_L - 6} y={y + 3} textAnchor="end" fontSize={9} fill="var(--color-text-faint)" fontFamily="var(--font-mono)">
                {t}
              </text>
            </g>
          );
        })}
        {buckets.map((b, i) => {
          const x = PAD_L + i * slot + (slot - barW) / 2;
          const okCount = b.run_count - b.failed_count;
          const okH = (okCount / maxRuns) * innerH;
          const failH = (b.failed_count / maxRuns) * innerH;
          const failY = PAD_T + innerH - okH - failH - (failH > 0 && okH > 0 ? 2 : 0);
          return (
            <g
              key={b.bucket}
              onMouseMove={(e) => {
                const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                setTip({
                  x: e.clientX - rect.left,
                  y: e.clientY - rect.top,
                  lines: [b.bucket, `runs ${b.run_count}`, `failed ${b.failed_count}`],
                });
              }}
              onMouseLeave={() => setTip(null)}
            >
              {/* hit target wider than the mark */}
              <rect x={PAD_L + i * slot} y={PAD_T} width={slot} height={innerH} fill="transparent" />
              {okCount > 0 && (
                <rect
                  x={x}
                  y={PAD_T + innerH - okH}
                  width={barW}
                  height={okH}
                  fill={C_PRIMARY}
                  rx={b.failed_count === 0 ? 3 : 0}
                />
              )}
              {b.failed_count > 0 && (
                <rect x={x} y={Math.max(PAD_T, failY)} width={barW} height={failH} fill={C_FAILED} rx={3} />
              )}
              {(i === 0 || i === buckets.length - 1) && (
                <text
                  x={x + barW / 2}
                  y={H - 8}
                  textAnchor="middle"
                  fontSize={9}
                  fill="var(--color-text-faint)"
                  fontFamily="var(--font-mono)"
                >
                  {b.bucket.slice(5)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <ChartTooltip tip={tip} />
    </div>
  );
}

/** Duration percentile lines: p50 + p95, 2px strokes, crosshair tooltip. */
function DurationLines({ buckets }: { buckets: AnalyticsBucket[] }) {
  const [tip, setTip] = useState<TooltipState | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const withDur = buckets.filter((b) => b.duration_ms_p50 != null);
  const maxDur = Math.max(1, ...withDur.map((b) => b.duration_ms_p95 ?? b.duration_ms_p50 ?? 0));
  const innerW = W - PAD_L - 8;
  const innerH = H - PAD_T - PAD_B;
  const xOf = (i: number) => PAD_L + (buckets.length <= 1 ? innerW / 2 : (i / (buckets.length - 1)) * innerW);
  const yOf = (v: number) => PAD_T + innerH * (1 - v / maxDur);

  const path = (key: 'duration_ms_p50' | 'duration_ms_p95') =>
    buckets
      .map((b, i) => (b[key] == null ? null : `${b[key] === null ? '' : ''}${xOf(i)},${yOf(b[key] as number)}`))
      .filter(Boolean)
      .map((pt, j) => `${j === 0 ? 'M' : 'L'}${pt}`)
      .join(' ');

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        role="img"
        aria-label="Run duration percentiles per day"
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - rect.left) / rect.width) * W;
          const i = Math.round(((px - PAD_L) / innerW) * Math.max(1, buckets.length - 1));
          const idx = Math.max(0, Math.min(buckets.length - 1, i));
          const b = buckets[idx];
          if (!b) return;
          setHoverIdx(idx);
          setTip({
            x: e.clientX - rect.left,
            y: e.clientY - rect.top,
            lines: [b.bucket, `p50 ${fmtMs(b.duration_ms_p50)}`, `p95 ${fmtMs(b.duration_ms_p95)}`],
          });
        }}
        onMouseLeave={() => {
          setTip(null);
          setHoverIdx(null);
        }}
      >
        {[0, 0.5, 1].map((f) => {
          const y = PAD_T + innerH * (1 - f);
          return (
            <g key={f}>
              <line x1={PAD_L} x2={W - 8} y1={y} y2={y} stroke="var(--color-border)" strokeWidth={1} />
              <text x={PAD_L - 6} y={y + 3} textAnchor="end" fontSize={9} fill="var(--color-text-faint)" fontFamily="var(--font-mono)">
                {fmtMs(maxDur * f)}
              </text>
            </g>
          );
        })}
        {hoverIdx != null && (
          <line x1={xOf(hoverIdx)} x2={xOf(hoverIdx)} y1={PAD_T} y2={PAD_T + innerH} stroke="var(--color-border-strong)" strokeWidth={1} />
        )}
        <path d={path('duration_ms_p95')} fill="none" stroke={C_P95} strokeWidth={2} strokeLinejoin="round" />
        <path d={path('duration_ms_p50')} fill="none" stroke={C_PRIMARY} strokeWidth={2} strokeLinejoin="round" />
        {hoverIdx != null &&
          (['duration_ms_p50', 'duration_ms_p95'] as const).map((k) => {
            const b = buckets[hoverIdx];
            if (b?.[k] == null) return null;
            return (
              <circle
                key={k}
                cx={xOf(hoverIdx)}
                cy={yOf(b[k] as number)}
                r={4}
                fill={k === 'duration_ms_p50' ? C_PRIMARY : C_P95}
                stroke="var(--color-bg-raised)"
                strokeWidth={2}
              />
            );
          })}
      </svg>
      <ChartTooltip tip={tip} />
    </div>
  );
}

function BucketsTable({ buckets }: { buckets: AnalyticsBucket[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px] font-mono">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
            <th className="text-left px-3 py-1.5">day</th>
            <th className="text-right px-3 py-1.5">runs</th>
            <th className="text-right px-3 py-1.5">failed</th>
            <th className="text-right px-3 py-1.5">model calls</th>
            <th className="text-right px-3 py-1.5">tool calls</th>
            <th className="text-right px-3 py-1.5">p50</th>
            <th className="text-right px-3 py-1.5">p95</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((b) => (
            <tr key={b.bucket} className="border-b border-[var(--color-border)] last:border-b-0">
              <td className="px-3 py-1.5 text-[var(--color-text)]">{b.bucket}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{b.run_count}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{b.failed_count}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{b.model_call_count}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{b.tool_call_count}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{fmtMs(b.duration_ms_p50)}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{fmtMs(b.duration_ms_p95)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const DEFAULT_QUERY_TEXT = JSON.stringify(
  { select: ['count()', 'avg(cost) AS avg_cost'], group_by: ['status'], since: '7d' },
  null,
  2,
);

/** Custom query panel (P4): a JSON/YAML Capsule Query DSL document, executed
 * via `POST /api/query` (the same closed allow-list `nova query` uses — no
 * raw SQL, no new grammar). Results render in the shared DataTable; a
 * "copy as CLI" chip lets an operator paste the equivalent `nova query …`
 * invocation into a terminal. */
function CustomQueryPanel() {
  const [text, setText] = useState(DEFAULT_QUERY_TEXT);
  const [engine, setEngine] = useState('');
  const [result, setResult] = useState<QueryPanelResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setResult(await api.runQuery(text, engine.trim() || undefined));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, [text, engine]);

  const columns: Column<Record<string, unknown>>[] = useMemo(
    () =>
      (result?.columns ?? []).map((c) => ({
        key: c,
        header: c,
        render: (row) => {
          const v = row[c];
          if (v == null) return '—';
          return typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(4)) : String(v);
        },
      })),
    [result],
  );

  const copyCli = useCallback(() => {
    if (!result) return;
    navigator.clipboard
      .writeText(result.cli_equivalent)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  }, [result]);

  return (
    <div className="space-y-3 rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3">
      <div className="flex items-center justify-between">
        <span className={labelClass}>Custom query (ADR-0129 DSL — JSON or YAML)</span>
        <input
          value={engine}
          onChange={(e) => setEngine(e.target.value)}
          placeholder="engine (auto)"
          className="w-32 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1 text-[10px] font-mono text-[var(--color-text)] focus:border-[var(--color-accent)] focus:outline-none"
        />
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        spellCheck={false}
        className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-[11px] text-[var(--color-text)] focus:border-[var(--color-accent)] focus:outline-none"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={() => void run()}
          disabled={loading}
          className="rounded border border-[var(--color-accent)] px-3 py-1 text-[11px] font-mono text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? 'running…' : 'Run query'}
        </button>
        {result && (
          <button
            onClick={copyCli}
            className="rounded border border-[var(--color-border)] px-2 py-1 text-[10px] font-mono text-[var(--color-text-muted)] hover:border-[var(--color-border-strong)]"
            title={result.cli_equivalent}
          >
            {copied ? 'copied ✓' : `copy as CLI`}
          </button>
        )}
      </div>
      {error && <p className="text-[11px] font-mono text-[var(--color-status-failure)]">{error}</p>}
      {result && (
        <>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            {result.row_count} row(s){result.truncated ? ' (truncated)' : ''} —{' '}
            {result.index.capsule_count} capsule(s) scanned, engine {result.index.engine}
          </p>
          <DataTable
            columns={columns}
            rows={result.rows}
            rowKey={(_row, i) => String(i)}
            maxHeight={320}
          />
        </>
      )}
    </div>
  );
}

export default function AnalyticsTab() {
  const [days, setDays] = useState<RangeDays>(30);
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showTable, setShowTable] = useState(false);

  const load = useCallback(async () => {
    try {
      setError(null);
      setData(await api.analyticsSummary({ since: isoDaysAgo(days) }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  const overall = useMemo(() => {
    if (!data || data.totals.run_count === 0) return null;
    const failRate = (data.totals.failed_count / data.totals.run_count) * 100;
    const p95s = data.buckets.map((b) => b.duration_ms_p95).filter((v): v is number => v != null);
    return {
      failRate: `${failRate.toFixed(1)}%`,
      worstP95: p95s.length ? fmtMs(Math.max(...p95s)) : '—',
    };
  }, [data]);

  return (
    <TabShell
      title="Analytics"
      subtitle="Run volume, failure rate, and latency percentiles from the local runs index"
      cli={['nova query', 'nova trend']}
      help="Aggregates come from the runs index (no capsule scans). Token/cost analytics stay in `nova query`/`nova cost` until model-call aggregates land server-side."
      actions={
        <div className="flex items-center gap-1">
          {RANGE_DAYS.map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`rounded border px-2 py-1 text-[10px] font-mono ${
                days === d
                  ? 'border-[var(--color-accent)] text-[var(--color-accent)]'
                  : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-border-strong)]'
              }`}
            >
              {d}d
            </button>
          ))}
          <button
            onClick={() => setShowTable((s) => !s)}
            className="ml-2 rounded border border-[var(--color-border)] px-2 py-1 text-[10px] font-mono text-[var(--color-text-muted)] hover:border-[var(--color-border-strong)]"
          >
            {showTable ? 'charts' : 'table'}
          </button>
        </div>
      }
    >
      {error && (
        <p className="text-[11px] font-mono text-[var(--color-status-failure)]">{error}</p>
      )}
      {data && data.buckets.length === 0 && !error && (
        <p className="text-[11px] font-mono text-[var(--color-text-faint)]">
          No runs in the selected window. Capture a run with `nova capture` and it will appear here.
        </p>
      )}
      {data && data.buckets.length > 0 && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile label="runs" value={String(data.totals.run_count)} sub={`last ${days} days`} />
            <StatTile label="failure rate" value={overall?.failRate ?? '—'} sub={`${data.totals.failed_count} failed`} />
            <StatTile label="model calls" value={String(data.totals.model_call_count)} sub={`${data.totals.tool_call_count} tool calls`} />
            <StatTile label="worst daily p95" value={overall?.worstP95 ?? '—'} sub="run duration" />
          </div>
          {showTable ? (
            <BucketsTable buckets={data.buckets} />
          ) : (
            <>
              <ChartCard
                title="runs per day"
                filename="analytics-runs-per-day"
                legend={
                  <span className="flex gap-3">
                    <LegendChip color={C_PRIMARY} label="completed" />
                    <LegendChip color={C_FAILED} label="failed" />
                  </span>
                }
              >
                <RunVolumeBars buckets={data.buckets} />
              </ChartCard>
              <ChartCard
                title="run duration percentiles"
                filename="analytics-duration-percentiles"
                legend={
                  <span className="flex gap-3">
                    <LegendChip color={C_PRIMARY} label="p50" />
                    <LegendChip color={C_P95} label="p95" />
                  </span>
                }
              >
                <DurationLines buckets={data.buckets} />
              </ChartCard>
            </>
          )}
        </div>
      )}
      <CustomQueryPanel />
    </TabShell>
  );
}
