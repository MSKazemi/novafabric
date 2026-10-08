/**
 * Pure helpers for the Dashboards view (ADR-0235 widgets, ADR-0236 ratio()).
 *
 * The one rule everything here serves — ADR-0234 D2 / ADR-0236 D4:
 * **absent or undefined is not zero, and zero is still zero.** `null`
 * (a ratio over a zero denominator, a sum over no priced rows) renders as
 * "no value" and is never plotted at the baseline; a measured `0` renders as
 * `0`. Nothing in this file may coerce one into the other.
 */
import type {
  DashboardWidgetRef,
  DerivedColumn,
  WidgetDataResponse,
  WidgetPresentation,
} from '../../../lib/dashboardTypes';
import { formatSI } from '../../../lib/chartFormat';

/** The chart kinds this build can draw (the v1 schema enum). */
export const KNOWN_CHARTS = ['line', 'bar', 'area', 'table', 'stat'] as const;

/** Text shown wherever a value is absent/undefined. Never "0". */
export const NO_VALUE = '—';
export const NO_VALUE_LABEL = 'no value';
export const NO_VALUE_EXPLAINED =
  'No value: undefined (e.g. a ratio over a zero denominator) or absent (nothing was measured). Not zero.';

/**
 * Series palette — categorical tokens tuned per theme. Status colours are
 * reserved for status and never used for a data series.
 */
export const SERIES_COLORS = [
  'var(--color-accent)',
  'var(--color-edge-derived-from)',
  'var(--color-edge-consumed)',
  'var(--color-edge-produced-by)',
  'var(--color-edge-evaluated-by)',
  'var(--color-edge-attested-by)',
];

/** A number, or `null` when absent / non-numeric. `0` stays `0`. */
export function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'boolean') return null;
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function isAbsent(value: unknown): boolean {
  return value === null || value === undefined;
}

/** Human-readable value with an optional unit; `null` → the no-value dash. */
export function formatValue(value: unknown, unit?: string): string {
  if (isAbsent(value)) return NO_VALUE;
  const n = toNumber(value);
  if (n === null) return String(value);
  let text: string;
  if (n === 0) text = '0';
  else if (Number.isInteger(n)) text = Math.abs(n) >= 10_000 ? formatSI(n) : n.toLocaleString('en-US');
  else if (Math.abs(n) >= 1000) text = formatSI(n);
  else if (Math.abs(n) >= 1) text = n.toFixed(2).replace(/\.?0+$/, '');
  else text = n.toPrecision(3).replace(/\.?0+$/, '');
  return unit ? `${text} ${unit}` : text;
}

/** The group-by dimensions of the widget's query (columns that are not measures). */
export function groupByOf(data: Pick<WidgetDataResponse, 'query'>): string[] {
  const g = data.query?.group_by;
  if (Array.isArray(g)) return g.map(String);
  if (typeof g === 'string' && g.trim()) return g.split(',').map((s) => s.trim()).filter(Boolean);
  return [];
}

/** Columns holding measures (aggregates and derived ratios), in select order. */
export function measureColumns(data: Pick<WidgetDataResponse, 'columns' | 'query'>): string[] {
  const dims = new Set(groupByOf(data));
  return data.columns.filter((c) => !dims.has(c));
}

export function derivedFor(data: Pick<WidgetDataResponse, 'derived'>, column: string): DerivedColumn | undefined {
  return data.derived.find((d) => d.alias === column);
}

/**
 * The measure a chart or stat shows by default: the first ratio when the
 * widget defines one (it is usually the point of the widget), else the first
 * measure. The viewer can switch; the choice is never silent.
 */
export function defaultMeasure(data: Pick<WidgetDataResponse, 'columns' | 'query' | 'derived'>): string | null {
  const measures = measureColumns(data);
  const ratio = measures.find((m) => data.derived.some((d) => d.alias === m));
  return ratio ?? measures[0] ?? null;
}

export interface RatioParts {
  numerator: { column: string; value: unknown };
  denominator: { column: string; value: unknown };
  /** Why the ratio has no value, when it has none. */
  reason: string | null;
}

/** The operands behind one ratio cell, and — when it is undefined — why. */
export function ratioParts(derived: DerivedColumn, row: Record<string, unknown>): RatioParts {
  const num = row[derived.numerator];
  const den = row[derived.denominator];
  let reason: string | null = null;
  if (isAbsent(row[derived.alias])) {
    if (isAbsent(num) && isAbsent(den)) reason = 'numerator and denominator absent';
    else if (isAbsent(num)) reason = 'numerator absent';
    else if (isAbsent(den)) reason = 'denominator absent';
    else if (toNumber(den) === 0) reason = 'zero denominator — undefined, not 0';
    else reason = 'operand not numeric';
  }
  return {
    numerator: { column: derived.numerator, value: num },
    denominator: { column: derived.denominator, value: den },
    reason,
  };
}

export interface ChartPoint {
  /** Category label on the x axis. */
  x: string;
  /** `null` = no value: drawn as a gap, never at the baseline. */
  y: number | null;
  row: Record<string, unknown>;
}

export interface ChartSeries {
  name: string;
  points: ChartPoint[];
}

export interface ChartModel {
  categories: string[];
  series: ChartSeries[];
  /** Points with no value — reported beside the chart, not hidden. */
  absent: number;
}

function label(row: Record<string, unknown>, dims: string[], fallback: number): string {
  if (dims.length === 0) return fallback === 0 ? 'all' : `#${fallback + 1}`;
  return dims.map((d) => (isAbsent(row[d]) ? '(none)' : String(row[d]))).join(' · ');
}

/**
 * Shape rows into categories × series for one measure.
 *
 * `presentation.breakdown`, when it names a group-by dimension and at least
 * one other dimension exists, pivots that dimension into series. Otherwise
 * there is one series, named after the measure.
 */
export function buildChartModel(
  data: Pick<WidgetDataResponse, 'columns' | 'rows' | 'query'>,
  presentation: WidgetPresentation,
  measure: string,
): ChartModel {
  const dims = groupByOf(data);
  const breakdown = presentation.breakdown;
  const pivot = !!breakdown && dims.includes(breakdown) && dims.length >= 2;
  const xDims = pivot ? dims.filter((d) => d !== breakdown) : dims;

  const categories: string[] = [];
  const seriesMap = new Map<string, Map<string, ChartPoint>>();
  data.rows.forEach((row, i) => {
    const x = label(row, xDims, i);
    if (!categories.includes(x)) categories.push(x);
    const name = pivot ? (isAbsent(row[breakdown!]) ? '(none)' : String(row[breakdown!])) : measure;
    if (!seriesMap.has(name)) seriesMap.set(name, new Map());
    seriesMap.get(name)!.set(x, { x, y: toNumber(row[measure]), row });
  });

  let absent = 0;
  const series: ChartSeries[] = [...seriesMap.entries()].map(([name, byX]) => ({
    name,
    points: categories.map((x) => {
      const p = byX.get(x);
      if (!p || p.y === null) absent += 1;
      return p ?? { x, y: null, row: {} };
    }),
  }));
  return { categories, series, absent };
}

/** Ordered for reading: top-to-bottom, then left-to-right (DOM order = visual order). */
export function orderedRefs(refs: DashboardWidgetRef[]): DashboardWidgetRef[] {
  return refs
    .map((ref, i) => ({ ref, i }))
    .sort((a, b) => {
      const ay = a.ref.position?.y;
      const by = b.ref.position?.y;
      if (ay === undefined || by === undefined) return a.i - b.i;
      if (ay !== by) return ay - by;
      return (a.ref.position?.x ?? 0) - (b.ref.position?.x ?? 0) || a.i - b.i;
    })
    .map(({ ref }) => ref);
}

/** Grid span (1–12) for a widget; full width when unspecified. */
export function spanOf(ref: DashboardWidgetRef): number {
  const w = ref.position?.w;
  return typeof w === 'number' && w >= 1 && w <= 12 ? Math.round(w) : 12;
}

/** Trigger a browser download of `blob` as `filename`. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
