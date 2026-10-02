/**
 * Zero-dependency SVG chart for a widget (ADR-0201: no charting library).
 *
 * Built from React elements, never an HTML string: a widget is untrusted
 * input (ADR-0235 D7), and its group values become labels here.
 *
 * A point with no value is a **gap** — a line breaks, a bar is replaced by a
 * dashed "no value" marker — and is never drawn at the baseline, which would
 * read as a measured zero (ADR-0234 D2). A measured zero is drawn as zero.
 */
import { useId, type ReactElement } from 'react';
import { formatSI } from '../../../lib/chartFormat';
import { SERIES_COLORS, formatValue, type ChartModel } from './model';

const W = 640;
const H = 220;
const ML = 48;
const MR = 12;
const MT = 10;
const MB = 34;

export type DrawableChart = 'bar' | 'line' | 'area';

export default function WidgetChart({
  model,
  kind,
  stacked = false,
  unit,
  label,
}: {
  model: ChartModel;
  kind: DrawableChart;
  stacked?: boolean;
  unit?: string;
  /** Accessible name for the figure. */
  label: string;
}) {
  const titleId = useId();
  const { categories, series } = model;
  const stack = kind === 'bar' && stacked && series.length > 1;

  const values: number[] = [0];
  if (stack) {
    categories.forEach((_, ci) => {
      let pos = 0;
      let neg = 0;
      series.forEach((s) => {
        const v = s.points[ci]?.y;
        if (v === null || v === undefined) return;
        if (v >= 0) pos += v; else neg += v;
      });
      values.push(pos, neg);
    });
  } else {
    series.forEach((s) => s.points.forEach((p) => { if (p.y !== null) values.push(p.y); }));
  }
  const hi = Math.max(...values);
  const lo = Math.min(...values);
  const span = hi - lo || 1;

  const plotW = W - ML - MR;
  const plotH = H - MT - MB;
  const n = Math.max(categories.length, 1);
  const slot = plotW / n;
  const xOf = (i: number) => ML + (i + 0.5) * slot;
  const yOf = (v: number) => MT + plotH * (1 - (v - lo) / span);
  const color = (si: number) => SERIES_COLORS[si % SERIES_COLORS.length];

  const ticks = hi === lo ? [lo] : [lo, (lo + hi) / 2, hi];
  const labelIdx = n <= 6 ? categories.map((_, i) => i) : [0, Math.floor(n / 2), n - 1];

  const marks: ReactElement[] = [];
  if (kind === 'bar') {
    const groupW = Math.max(4, slot * 0.7);
    const barW = stack ? groupW : Math.max(2, groupW / series.length);
    categories.forEach((cat, ci) => {
      let posBase = 0;
      let negBase = 0;
      series.forEach((s, si) => {
        const p = s.points[ci];
        const x = stack ? xOf(ci) - groupW / 2 : xOf(ci) - groupW / 2 + si * barW;
        const name = `${cat} · ${s.name}`;
        if (!p || p.y === null) {
          if (stack) return;
          marks.push(
            <rect
              key={`na-${si}-${ci}`}
              x={x + 0.5}
              y={yOf(0) - 6}
              width={Math.max(barW - 1, 1)}
              height={6}
              fill="none"
              stroke="var(--color-text-faint)"
              strokeDasharray="2 2"
              data-testid="chart-no-value"
            >
              <title>{`${name}: no value (not zero)`}</title>
            </rect>,
          );
          return;
        }
        const v = p.y;
        const base = stack ? (v >= 0 ? posBase : negBase) : 0;
        const top = yOf(base + v);
        const bottom = yOf(base);
        if (stack) { if (v >= 0) posBase += v; else negBase += v; }
        marks.push(
          <rect
            key={`b-${si}-${ci}`}
            x={x}
            y={Math.min(top, bottom)}
            width={Math.max(barW - (stack ? 0 : 1), 1)}
            height={Math.max(Math.abs(bottom - top), v === 0 ? 0 : 1)}
            fill={color(si)}
          >
            <title>{`${name}: ${formatValue(v, unit)}`}</title>
          </rect>,
        );
      });
    });
  } else {
    series.forEach((s, si) => {
      // Split into runs of defined points: a gap is a break, never a dip to 0.
      const runs: Array<Array<{ x: number; y: number }>> = [];
      let current: Array<{ x: number; y: number }> = [];
      s.points.forEach((p, i) => {
        if (p.y === null) {
          if (current.length) runs.push(current);
          current = [];
          return;
        }
        current.push({ x: xOf(i), y: yOf(p.y) });
      });
      if (current.length) runs.push(current);
      runs.forEach((run, ri) => {
        const pts = run.map((pt) => `${pt.x.toFixed(1)},${pt.y.toFixed(1)}`).join(' ');
        if (kind === 'area' && run.length > 1) {
          const base = yOf(Math.max(lo, 0)).toFixed(1);
          marks.push(
            <polygon
              key={`a-${si}-${ri}`}
              points={`${run[0].x.toFixed(1)},${base} ${pts} ${run[run.length - 1].x.toFixed(1)},${base}`}
              fill={color(si)}
              opacity={0.18}
            />,
          );
        }
        if (run.length > 1) {
          marks.push(
            <polyline key={`l-${si}-${ri}`} points={pts} fill="none" stroke={color(si)} strokeWidth={2} />,
          );
        }
      });
      s.points.forEach((p, i) => {
        if (p.y === null) return;
        marks.push(
          <circle key={`p-${si}-${i}`} cx={xOf(i)} cy={yOf(p.y)} r={3} fill={color(si)}>
            <title>{`${p.x} · ${s.name}: ${formatValue(p.y, unit)}`}</title>
          </circle>,
        );
      });
    });
  }

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full h-auto text-[var(--color-text)]"
      role="img"
      aria-labelledby={titleId}
      data-testid="widget-chart"
    >
      <title id={titleId}>{label}</title>
      {ticks.map((v) => (
        <g key={`t-${v}`}>
          <line x1={ML} x2={W - MR} y1={yOf(v)} y2={yOf(v)} stroke="var(--color-border)" strokeWidth={1} />
          <text
            x={ML - 6}
            y={yOf(v) + 3}
            textAnchor="end"
            fontSize={9}
            fill="var(--color-text-faint)"
            fontFamily="var(--font-mono)"
          >
            {formatSI(Math.round(v * 1000) / 1000)}
          </text>
        </g>
      ))}
      {labelIdx.map((i) => (
        <text
          key={`x-${i}`}
          x={xOf(i)}
          y={H - 12}
          textAnchor="middle"
          fontSize={9}
          fill="var(--color-text-faint)"
          fontFamily="var(--font-mono)"
        >
          {categories[i].length > 18 ? `${categories[i].slice(0, 17)}…` : categories[i]}
        </text>
      ))}
      {marks}
    </svg>
  );
}
