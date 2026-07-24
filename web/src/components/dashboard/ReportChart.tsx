import { useMemo } from 'react';
import ChartCard from '../ui/ChartCard';

/** Chart spec served by GET /api/reports/catalog (ADR-0200/0201). */
export interface ReportChartSpec {
  kind: 'line' | 'bar' | 'stacked-bar' | 'multi-line';
  x: string;
  y: string[];
  colors: string[];
  title: string;
  requires_filters: string[];
}

const W = 640;
const HEIGHT = 200;
const ML = 44;
const MR = 8;
const MT = 8;
const MB = 30;

function num(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * Renders the registry-declared chart for a report's rows as inline SVG,
 * wrapped in ChartCard so the SVG/PNG image-export affordance applies.
 * Returns null when the spec's required filters are unmet or no numeric
 * series exists — charts are never fabricated (ADR-0201).
 */
export default function ReportChart({
  spec,
  rows,
  filters,
}: {
  spec: ReportChartSpec;
  rows: Record<string, unknown>[];
  filters: Record<string, string>;
}) {
  const chart = useMemo(() => {
    if (spec.requires_filters.some(f => !filters[f])) return null;
    if (rows.length === 0) return null;

    const labels = rows.map(r => String(r[spec.x] ?? ''));
    const seriesByKey = spec.y.map(k => rows.map(r => num(r[k])));
    const isLine = spec.kind === 'line' || spec.kind === 'multi-line';

    // Line charts read left-to-right in time; builders may emit newest-first.
    let order = rows.map((_, i) => i);
    if (isLine) order = [...order].sort((a, b) => labels[a].localeCompare(labels[b]));

    const totals =
      spec.kind === 'stacked-bar'
        ? order.map(i => seriesByKey.reduce((s, ser) => s + Math.max(0, ser[i] ?? 0), 0))
        : order.flatMap(i => seriesByKey.map(ser => ser[i]).filter((v): v is number => v !== null));
    const flat = totals.filter(v => Number.isFinite(v));
    if (flat.length === 0) return null;
    const hi = Math.max(...flat, 0) || 1;
    const lo = Math.min(...flat, 0);
    const span = hi - lo || 1;

    const plotW = W - ML - MR;
    const plotH = HEIGHT - MT - MB;
    const n = order.length;
    const slot = plotW / n;
    const xOf = (i: number) => ML + (i + 0.5) * slot;
    const yOf = (v: number) => MT + plotH * (1 - (v - lo) / span);
    const color = (ki: number) => spec.colors[ki % spec.colors.length] ?? '#3987e5';

    const marks: string[] = [];
    if (spec.kind === 'bar' || spec.kind === 'stacked-bar') {
      const barW = Math.max(2, slot * 0.6);
      order.forEach((ri, i) => {
        let yBase = yOf(0);
        spec.y.forEach((k, ki) => {
          const v = seriesByKey[ki][ri];
          if (v === null || v === 0) return;
          const h = Math.abs(yOf(0) - yOf(v));
          const yTop = spec.kind === 'stacked-bar' ? yBase - h : Math.min(yOf(v), yOf(0));
          marks.push(
            `<rect x="${(xOf(i) - barW / 2).toFixed(1)}" y="${yTop.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(h, 1).toFixed(1)}" fill="${color(ki)}"><title>${labels[ri]} · ${k}: ${v}</title></rect>`,
          );
          if (spec.kind === 'stacked-bar') yBase = yTop;
        });
      });
    } else {
      spec.y.forEach((k, ki) => {
        const pts = order
          .map((ri, i) => ({ v: seriesByKey[ki][ri], i }))
          .filter((p): p is { v: number; i: number } => p.v !== null)
          .map(p => `${xOf(p.i).toFixed(1)},${yOf(p.v).toFixed(1)}`);
        if (pts.length > 1) {
          marks.push(
            `<polyline points="${pts.join(' ')}" fill="none" stroke="${color(ki)}" stroke-width="2"><title>${k}</title></polyline>`,
          );
        }
      });
    }

    // Sparse x labels: first, middle, last.
    const labelIdx = n <= 3 ? order.map((_, i) => i) : [0, Math.floor(n / 2), n - 1];
    const xLabels = labelIdx
      .map(
        i =>
          `<text x="${xOf(i).toFixed(1)}" y="${HEIGHT - 8}" text-anchor="middle" font-size="9" fill="currentColor" opacity="0.6">${labels[order[i]].slice(0, 16)}</text>`,
      )
      .join('');
    const yTicks = [lo, (lo + hi) / 2, hi]
      .map(
        v =>
          `<text x="${ML - 6}" y="${(yOf(v) + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="currentColor" opacity="0.6">${Math.round(v * 100) / 100}</text>` +
          `<line x1="${ML}" x2="${W - MR}" y1="${yOf(v).toFixed(1)}" y2="${yOf(v).toFixed(1)}" stroke="currentColor" opacity="0.12"/>`,
      )
      .join('');

    return { svg: yTicks + xLabels + marks.join('') };
  }, [spec, rows, filters]);

  if (!chart) return null;

  return (
    <div className="mb-4 no-print-chart">
      <ChartCard
        title={spec.title}
        legend={
          spec.y.length > 1 ? (
            <span className="flex items-center gap-2 text-[10px] font-mono text-[var(--color-text-faint)]">
              {spec.y.map((k, ki) => (
                <span key={k} className="flex items-center gap-1">
                  <span
                    className="inline-block h-2 w-2 rounded-sm"
                    style={{ background: spec.colors[ki % spec.colors.length] }}
                    aria-hidden="true"
                  />
                  {k}
                </span>
              ))}
            </span>
          ) : undefined
        }
      >
        <svg
          viewBox={`0 0 ${W} ${HEIGHT}`}
          className="w-full text-[var(--color-text)]"
          role="img"
          aria-label={spec.title}
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: chart.svg }}
        />
      </ChartCard>
    </div>
  );
}
