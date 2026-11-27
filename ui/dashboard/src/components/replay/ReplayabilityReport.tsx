/**
 * The replayability report of one run (issue #269): what a strict mocked replay would
 * do, as `GET /api/runs/{id}/replayability` classified it, and the capture-time
 * prediction stored in the capsule. Missing data is shown as unknown, never invented.
 */
import { useId } from 'react';
import Badge from '../ui/primitives/Badge';
import type { ReplayabilitySummary, ReplayabilityView } from '../../lib/api';

const VERDICT: Record<ReplayabilitySummary['replayable'], { tone: 'success' | 'pending' | 'danger'; text: string }> = {
  full: { tone: 'success', text: 'a strict mocked replay would serve every recorded call' },
  partial: { tone: 'pending', text: 'some recorded calls would not be served' },
  none: { tone: 'danger', text: 'the capsule cannot be re-run' },
};

/** Count keys in the order they are worth reading; others follow. */
const COUNT_ORDER = ['recorded', 'servable', 'not_servable', 'covered', 'live', 'observed_only', 'not_intercepted'];
const COUNT_LABEL: Record<string, string> = {
  recorded: 'recorded',
  servable: 'servable',
  not_servable: 'not servable',
  covered: 'covered (inside a served tool call)',
  live: 'would run live',
  observed_only: 'observed only',
  not_intercepted: 'not intercepted (run live)',
};

function Counts({ label, counts }: { label: string; counts: Record<string, number> | undefined }) {
  const keys = Object.keys(counts ?? {}).sort((a, b) => {
    const ia = COUNT_ORDER.indexOf(a);
    const ib = COUNT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">{label}</dt>
      <dd className="mt-1">
        {keys.length === 0 ? (
          <span className="text-xs text-[var(--color-text-faint)]">not reported</span>
        ) : (
          <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs font-mono">
            {keys.map(k => (
              <li key={k}>
                <span className="text-[var(--color-text)]">{counts?.[k]}</span>{' '}
                <span className="text-[var(--color-text-muted)]">{COUNT_LABEL[k] ?? k.replace(/_/g, ' ')}</span>
              </li>
            ))}
          </ul>
        )}
      </dd>
    </div>
  );
}

function bySystem(reason: Record<string, unknown>): string | null {
  const by = reason.by_system;
  if (!by || typeof by !== 'object') return null;
  const parts = Object.entries(by as Record<string, unknown>).map(([system, v]) => {
    if (v && typeof v === 'object') {
      // {count, hosts} per gen_ai.system (replay/_servability.py:not_servable_by_system).
      const o = v as { count?: unknown; hosts?: unknown };
      const hosts = Array.isArray(o.hosts) && o.hosts.length ? o.hosts.join(', ') : 'host not recorded';
      return `${system}: ${String(o.count ?? '?')} (${hosts})`;
    }
    return `${system}: ${String(v)}`;
  });
  return parts.length ? parts.join('; ') : null;
}

export function SummaryBlock({ summary }: { summary: ReplayabilitySummary }) {
  const verdict = VERDICT[summary.replayable] ?? { tone: 'pending' as const, text: 'unrecognised verdict' };
  return (
    <div className="space-y-3">
      <dl className="grid gap-3 sm:grid-cols-2">
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">Replayable</dt>
          <dd className="mt-1"><Badge tone={verdict.tone}>{summary.replayable}</Badge>{' '}
            <span className="text-xs text-[var(--color-text-muted)]">{verdict.text}</span>
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">Strict mocked replay</dt>
          <dd className="mt-1 text-xs">
            {summary.strict_mocked === 'refuse' ? (
              <>
                <Badge tone="danger">would refuse</Badge>{' '}
                <span className="text-[var(--color-text-muted)]">before anything runs</span>
              </>
            ) : (
              <>
                <Badge tone="success">would start</Badge>{' '}
                <span className="text-[var(--color-text-muted)]">and fail closed on any divergence</span>
              </>
            )}
          </dd>
        </div>
        <Counts label="Model calls" counts={summary.model_calls} />
        <Counts label="Tool calls" counts={summary.tool_calls} />
      </dl>
      {summary.refusal && (
        <p className="text-xs text-[var(--color-text-muted)]">
          <span className="font-mono text-[var(--color-status-failure)]">{String(summary.refusal.type ?? 'refusal')}</span>
          {summary.refusal.guard ? <> (guard <code className="font-mono">{String(summary.refusal.guard)}</code>)</> : null}
          {summary.refusal.message ? <>: {String(summary.refusal.message)}</> : null}
        </p>
      )}
      {summary.reasons.length > 0 ? (
        <div>
          <h5 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-1">
            Why not every call is served
          </h5>
          <ul className="space-y-2">
            {summary.reasons.map((r, i) => {
              const systems = bySystem(r);
              return (
                <li key={`${r.code}-${i}`} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <code className="font-mono text-[var(--color-text)]">{r.code}</code>
                    <span className="text-[var(--color-text-faint)]">×{r.count}</span>
                    {r.refuses_strict
                      ? <Badge tone="danger">refuses strict replay</Badge>
                      : <Badge tone="pending">replay starts</Badge>}
                  </div>
                  <p className="mt-1 text-[var(--color-text-muted)]">{r.detail || r.summary}</p>
                  {systems && <p className="mt-1 font-mono text-[var(--color-text-faint)]">{systems}</p>}
                </li>
              );
            })}
          </ul>
        </div>
      ) : (
        <p className="text-xs text-[var(--color-text-muted)]">No reasons: every recorded call is servable.</p>
      )}
    </div>
  );
}

export interface ReplayabilityReportProps {
  view: ReplayabilityView | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

export default function ReplayabilityReport({ view, loading, error, onRetry }: ReplayabilityReportProps) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <h4 id={headingId} className="text-sm font-medium text-[var(--color-text)]">Replayability report</h4>
      {loading && <p role="status" aria-live="polite" className="text-xs text-[var(--color-text-muted)]">Classifying the capsule…</p>}
      {!loading && error && (
        <div role="alert" className="text-xs text-[var(--color-status-failure)] space-y-2">
          <p>Could not load the replayability report: {error}. Servability is unknown; the commands below still work.</p>
          <button
            type="button"
            onClick={onRetry}
            className="px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
          >
            Retry
          </button>
        </div>
      )}
      {!loading && !error && view && (
        <>
          <div>
            <p className="text-xs text-[var(--color-text-muted)] mb-2">
              Classified now by this server
              {view.current ? <> (novafabric <code className="font-mono">{view.current.classifier_version}</code>)</> : null}
              {' '}with the same rules <code className="font-mono">nova replay</code> applies. Nothing was executed.
            </p>
            {view.current
              ? <SummaryBlock summary={view.current} />
              : (
                <p role="alert" className="text-xs text-[var(--color-status-failure)]">
                  The capsule could not be classified: {view.current_error ?? 'unknown error'}. Servability is unknown.
                </p>
              )}
          </div>
          <div className="border-t border-[var(--color-border)] pt-3 text-xs text-[var(--color-text-muted)]">
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">At capture: </span>
            {view.recorded_state === 'present' && view.recorded && (
              <>
                the capsule's <code className="font-mono">replayability.json</code> (novafabric{' '}
                <code className="font-mono">{view.recorded.classifier_version}</code>) predicted{' '}
                <strong className="text-[var(--color-text)]">{view.recorded.replayable}</strong>
                {view.current && view.current.replayable !== view.recorded.replayable
                  ? <> — this differs from the current classification, which is what <code className="font-mono">nova replay</code> uses.</>
                  : '.'}
              </>
            )}
            {view.recorded_state === 'absent' && (
              <>this capsule was written without <code className="font-mono">replayability.json</code> (unknown at capture).</>
            )}
            {view.recorded_state === 'invalid' && (
              <>the capsule's <code className="font-mono">replayability.json</code> is unreadable: {view.recorded_error ?? 'invalid'}.</>
            )}
          </div>
        </>
      )}
    </section>
  );
}
