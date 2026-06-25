import { clsx } from 'clsx';

interface RawSpan {
  span_id?: string;
  parent?: string;
  name?: string;
  duration_ms?: number;
  kind?: string;
}

interface FlatSpan {
  path: string;   // e.g. "root/model_001"
  name: string;
  duration_ms: number;
  kind: string;
  depth: number;
}

function flattenSpans(spans: RawSpan[]): FlatSpan[] {
  const childrenOf = new Map<string, RawSpan[]>();
  let rootSpan: RawSpan | null = null;
  for (const s of spans) {
    if (!s.parent) { rootSpan = s; }
    else {
      if (!childrenOf.has(s.parent)) childrenOf.set(s.parent, []);
      childrenOf.get(s.parent)!.push(s);
    }
  }
  if (!rootSpan) return [];

  const result: FlatSpan[] = [];
  function walk(sp: RawSpan, parentPath: string, depth: number) {
    const path = parentPath ? `${parentPath}/${sp.name ?? '?'}` : (sp.name ?? 'root');
    result.push({ path, name: sp.name ?? '?', duration_ms: sp.duration_ms ?? 0, kind: sp.kind ?? 'internal', depth });
    for (const child of childrenOf.get(sp.span_id ?? '') ?? []) {
      walk(child, path, depth + 1);
    }
  }
  walk(rootSpan, '', 0);
  return result;
}

type DiffKind = 'same' | 'changed' | 'added' | 'removed';

interface SpanDiff {
  name: string;
  depth: number;
  kind: DiffKind;
  a?: FlatSpan;
  b?: FlatSpan;
  durationDelta?: number; // ms
  durationPct?: number;   // % change
}

function diffSpans(aSpans: FlatSpan[], bSpans: FlatSpan[]): SpanDiff[] {
  const aByPath = new Map(aSpans.map(s => [s.path, s]));
  const bByPath = new Map(bSpans.map(s => [s.path, s]));
  const allPaths = new Set([...aByPath.keys(), ...bByPath.keys()]);

  // Stable order: use A ordering first, then B additions
  const orderedPaths: string[] = [];
  for (const s of aSpans) orderedPaths.push(s.path);
  for (const s of bSpans) if (!aByPath.has(s.path)) orderedPaths.push(s.path);

  return orderedPaths.map(path => {
    const a = aByPath.get(path);
    const b = bByPath.get(path);
    const name = (a ?? b)!.name;
    const depth = (a ?? b)!.depth;

    if (a && b) {
      const delta = b.duration_ms - a.duration_ms;
      const pct = a.duration_ms > 0 ? (delta / a.duration_ms) * 100 : 0;
      const changed = Math.abs(delta) > 50 || Math.abs(pct) > 10;
      return { name, depth, kind: changed ? 'changed' : 'same', a, b, durationDelta: delta, durationPct: pct };
    }
    if (a && !b) return { name, depth, kind: 'removed', a };
    return { name, depth, kind: 'added', b };
  });
}

function fmtMs(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

function DiffBadge({ kind, delta, pct }: { kind: DiffKind; delta?: number; pct?: number }) {
  if (kind === 'added') return (
    <span className="text-[9px] uppercase tracking-wider px-1.5 py-px rounded bg-[color-mix(in_oklab,var(--color-status-success)_15%,transparent)] text-[var(--color-status-success)] font-medium">
      +added
    </span>
  );
  if (kind === 'removed') return (
    <span className="text-[9px] uppercase tracking-wider px-1.5 py-px rounded bg-[color-mix(in_oklab,var(--color-status-failure)_15%,transparent)] text-[var(--color-status-failure)] font-medium">
      −removed
    </span>
  );
  if (kind === 'changed' && delta != null) return (
    <span className={clsx(
      'text-[9px] font-mono tabular-nums px-1.5 py-px rounded font-medium',
      delta > 0
        ? 'bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] text-[var(--color-status-failure)]'
        : 'bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] text-[var(--color-status-success)]',
    )}>
      {delta > 0 ? '+' : ''}{fmtMs(delta)} ({pct != null ? `${delta > 0 ? '+' : ''}${pct.toFixed(0)}%` : '?'})
    </span>
  );
  return null;
}

interface TraceDiffGraphProps {
  runAId: string;
  runBId: string;
  traceA: unknown[];
  traceB: unknown[];
  modelCallsA: unknown[];
  modelCallsB: unknown[];
}

export default function TraceDiffGraph({ runAId, runBId, traceA, traceB, modelCallsA, modelCallsB }: TraceDiffGraphProps) {
  const aSpans = flattenSpans(traceA as RawSpan[]);
  const bSpans = flattenSpans(traceB as RawSpan[]);
  const diffs = diffSpans(aSpans, bSpans);

  const aRoot = aSpans[0];
  const bRoot = bSpans[0];
  const totalDelta = aRoot && bRoot ? bRoot.duration_ms - aRoot.duration_ms : null;

  const added   = diffs.filter(d => d.kind === 'added').length;
  const removed = diffs.filter(d => d.kind === 'removed').length;
  const changed = diffs.filter(d => d.kind === 'changed').length;

  if (diffs.length === 0) {
    return <p className="text-sm text-[var(--color-text-muted)] p-4">No trace data available for one or both runs.</p>;
  }

  return (
    <div>
      {/* Summary */}
      <div className="flex flex-wrap items-center gap-4 mb-4 text-xs">
        <div className="font-mono text-[var(--color-text-faint)]">
          <span className="text-[var(--color-text)]">{runAId.slice(0, 12)}…</span> → <span className="text-[var(--color-text)]">{runBId.slice(0, 12)}…</span>
        </div>
        {totalDelta != null && (
          <span className={clsx('font-mono tabular-nums font-medium', totalDelta > 0 ? 'text-[var(--color-status-failure)]' : 'text-[var(--color-status-success)]')}>
            total: {totalDelta > 0 ? '+' : ''}{fmtMs(totalDelta)}
          </span>
        )}
        {changed > 0 && <span className="text-[var(--color-status-pending)]">{changed} changed</span>}
        {added   > 0 && <span className="text-[var(--color-status-success)]">+{added} added</span>}
        {removed > 0 && <span className="text-[var(--color-status-failure)]">−{removed} removed</span>}
        {added === 0 && removed === 0 && changed === 0 && (
          <span className="text-[var(--color-text-faint)]">traces are structurally identical</span>
        )}
      </div>

      {/* Span-by-span comparison */}
      <div className="rounded border border-[var(--color-border)] overflow-hidden">
        {/* Header */}
        <div className="grid grid-cols-[220px_1fr_1fr_160px] text-[9px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium border-b border-[var(--color-border)] bg-[var(--color-bg-sunken)]">
          <div className="px-3 py-2">Span</div>
          <div className="px-3 py-2 text-right border-l border-[var(--color-border)]">A · {runAId.slice(0, 8)}…</div>
          <div className="px-3 py-2 text-right border-l border-[var(--color-border)]">B · {runBId.slice(0, 8)}…</div>
          <div className="px-3 py-2 border-l border-[var(--color-border)]">Delta</div>
        </div>

        {diffs.map((d, i) => (
          <div
            key={d.name + i}
            className={clsx(
              'grid grid-cols-[220px_1fr_1fr_160px] items-center text-xs border-b border-[var(--color-border)] last:border-0 transition-colors',
              d.kind === 'added'   && 'bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)]',
              d.kind === 'removed' && 'bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)]',
              d.kind === 'changed' && 'bg-[color-mix(in_oklab,var(--color-status-pending)_4%,transparent)]',
              d.kind === 'same'    && 'hover:bg-[var(--color-bg-sunken)]',
            )}
          >
            {/* Name */}
            <div className="px-2 py-2 font-mono truncate" style={{ paddingLeft: `${d.depth * 12 + 8}px` }}>
              <span className={clsx(d.depth === 0 ? 'text-[var(--color-text)] font-medium' : 'text-[var(--color-text-muted)]')}>
                {d.name}
              </span>
            </div>

            {/* A duration */}
            <div className="px-3 py-2 text-right border-l border-[var(--color-border)] font-mono tabular-nums">
              {d.a ? (
                <span className={clsx(d.kind === 'removed' ? 'text-[var(--color-status-failure)] line-through' : 'text-[var(--color-text-muted)]')}>
                  {fmtMs(d.a.duration_ms)}
                </span>
              ) : <span className="text-[var(--color-text-faint)]">—</span>}
            </div>

            {/* B duration */}
            <div className="px-3 py-2 text-right border-l border-[var(--color-border)] font-mono tabular-nums">
              {d.b ? (
                <span className={clsx(d.kind === 'added' ? 'text-[var(--color-status-success)]' : 'text-[var(--color-text-muted)]')}>
                  {fmtMs(d.b.duration_ms)}
                </span>
              ) : <span className="text-[var(--color-text-faint)]">—</span>}
            </div>

            {/* Delta */}
            <div className="px-3 py-2 border-l border-[var(--color-border)]">
              <DiffBadge kind={d.kind} delta={d.durationDelta} pct={d.durationPct} />
            </div>
          </div>
        ))}
      </div>

      <p className="mt-3 text-[10px] text-[var(--color-text-faint)] font-mono">
        Spans matched by tree path. Duration delta &gt; 50ms or &gt; 10% flagged as changed.
      </p>
    </div>
  );
}
