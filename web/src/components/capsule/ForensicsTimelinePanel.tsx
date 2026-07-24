/**
 * ForensicsTimelinePanel — the P5 read surface for
 * ``GET /api/runs/{run_id}/forensics-timeline`` (ADR-0155 follow-on slice).
 *
 * Renders the deterministic {@link ForensicsTimeline} the server reconstructs
 * from the run's own sealed capsule via the same ``merge_timeline`` core
 * ``nova forensics timeline`` uses. Honesty is load-bearing here: the panel
 * surfaces the server's ``honesty_line`` and every ``gap`` verbatim — it never
 * hides that lineage evidence has no collector yet, and it never fabricates an
 * event for a record the server reported as a gap.
 */
import { clsx } from 'clsx';
import type { ForensicsTimeline } from '../../lib/api';

const KIND_COLOR: Record<string, string> = {
  run: 'var(--color-accent)',
  'model-call': 'var(--color-status-success)',
  'tool-call': 'var(--color-text-muted)',
};

function kindColor(kind: string): string {
  return KIND_COLOR[kind] ?? 'var(--color-text-faint)';
}

export default function ForensicsTimelinePanel({
  data,
  loading,
  error,
}: {
  data: ForensicsTimeline | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading) {
    return <p className="text-sm text-[var(--color-text-muted)] py-6">Reconstructing timeline…</p>;
  }
  if (error) {
    return (
      <p className="text-sm text-[var(--color-status-failure)] py-6 font-mono break-all">{error}</p>
    );
  }
  if (!data) return null;

  return (
    <div className="space-y-4">
      {/* Honest scope banner — the server's own contract line, shown verbatim. */}
      <p className="text-[11px] leading-relaxed text-[var(--color-text-muted)] italic border-l-2 border-[var(--color-border)] pl-2">
        {data.honesty_line}
      </p>

      {data.events.length === 0 ? (
        <p className="text-sm text-[var(--color-text-muted)]">
          No timestamped events could be reconstructed from this capsule.
        </p>
      ) : (
        <ol className="relative border-l border-[var(--color-border)] ml-2 space-y-3">
          {data.events.map((ev, i) => (
            <li key={`${ev.source_capsule}:${ev.seq}:${i}`} className="ml-4">
              <span
                className="absolute -left-[5px] w-2.5 h-2.5 rounded-full border border-[var(--color-bg)]"
                style={{ backgroundColor: kindColor(ev.kind) }}
                aria-hidden
              />
              <div className="flex items-baseline gap-2 flex-wrap">
                <code className="font-mono text-xs text-[var(--color-text)]">{ev.ts}</code>
                <span
                  className="text-[10px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded"
                  style={{ color: kindColor(ev.kind), backgroundColor: 'var(--color-bg-sunken)' }}
                >
                  {ev.kind}
                </span>
              </div>
              {ev.detail && (
                <p className="text-xs text-[var(--color-text-muted)] mt-0.5 break-all">{ev.detail}</p>
              )}
            </li>
          ))}
        </ol>
      )}

      {data.gaps.length > 0 && (
        <div>
          <h4 className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-text-muted)] mb-1">
            Gaps ({data.gaps.length})
          </h4>
          <ul className="space-y-1">
            {data.gaps.map((gap, i) => (
              <li
                key={i}
                className={clsx(
                  'text-xs font-mono break-all pl-2 border-l-2',
                  'border-[var(--color-status-warning,var(--color-accent))] text-[var(--color-text-muted)]',
                )}
              >
                {gap}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
