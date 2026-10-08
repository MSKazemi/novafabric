/**
 * Pure helpers for the dashboard/widget editor.
 *
 * Nothing here decides whether a document is *valid*: ids, chart kinds and the
 * query grammar belong to the server, which runs the same loaders as
 * `nova dashboard apply` (ADR-0235 D7). This file only shapes input and
 * presents the server's verdict.
 */

export type DiffKind = 'same' | 'add' | 'del';
export interface DiffLine {
  kind: DiffKind;
  text: string;
}

/** Above this many cells the exact diff is skipped and the file reads as replaced. */
const DIFF_CELL_LIMIT = 4_000_000;

/**
 * Line diff of `before` (what is on disk; `null` = no file) against `after`
 * (what the server would store). Whether anything changes is the server's
 * `action`; this only draws it.
 */
export function lineDiff(before: string | null, after: string): DiffLine[] {
  const a = before === null ? [] : before.replace(/\n$/, '').split('\n');
  const b = after.replace(/\n$/, '').split('\n');
  if (a.length * b.length > DIFF_CELL_LIMIT) {
    return [
      ...a.map((text) => ({ kind: 'del' as const, text })),
      ...b.map((text) => ({ kind: 'add' as const, text })),
    ];
  }
  const w = b.length + 1;
  const lcs = new Uint32Array((a.length + 1) * w);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i * w + j] =
        a[i] === b[j]
          ? lcs[(i + 1) * w + j + 1]! + 1
          : Math.max(lcs[(i + 1) * w + j]!, lcs[i * w + j + 1]!);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: 'same', text: a[i]! });
      i++;
      j++;
    } else if (lcs[(i + 1) * w + j]! >= lcs[i * w + j + 1]!) {
      out.push({ kind: 'del', text: a[i++]! });
    } else {
      out.push({ kind: 'add', text: b[j++]! });
    }
  }
  while (i < a.length) out.push({ kind: 'del', text: a[i++]! });
  while (j < b.length) out.push({ kind: 'add', text: b[j++]! });
  return out;
}

export interface WidgetFields {
  id: string;
  title: string;
  description: string;
  chart: string;
  unit: string;
  breakdown: string;
  stacked: boolean;
  /** The query object as JSON text; the server's DSL decides whether it is acceptable. */
  query: string;
}

export const EMPTY_WIDGET_FIELDS: WidgetFields = {
  id: '',
  title: '',
  description: '',
  chart: 'bar',
  unit: '',
  breakdown: '',
  stacked: false,
  query: '{\n  "select": ["count()"],\n  "group_by": ["status"],\n  "since": "7d"\n}',
};

/**
 * Compose the guided fields into widget JSON *text* for the server to judge.
 * Only syntax is handled here (is the query box JSON at all).
 */
export function composeWidgetText(f: WidgetFields): { text: string } | { error: string } {
  let query: unknown;
  try {
    query = JSON.parse(f.query);
  } catch (e) {
    return { error: `The query box is not JSON: ${e instanceof Error ? e.message : String(e)}` };
  }
  const presentation: Record<string, unknown> = { chart: f.chart };
  if (f.unit) presentation.unit = f.unit;
  if (f.breakdown) presentation.breakdown = f.breakdown;
  if (f.stacked) presentation.stacked = true;
  const doc: Record<string, unknown> = {
    $novafabricWidget: true,
    version: 1,
    id: f.id,
    title: f.title,
    ...(f.description ? { description: f.description } : {}),
    query,
    presentation,
  };
  return { text: JSON.stringify(doc, null, 2) };
}
