/**
 * Failure-first surfaces for the Runs view (issue #268, first slice).
 *
 * - `RunFailureBanner` — at the top of the inspector of a run that did not
 *   succeed: its recorded status, exit code and manifest `error`, and the first
 *   observed failing event with a button that opens it in the Events view.
 * - `FailedRunsStrip` — above the run list: the newest runs whose status is
 *   `failure` or `error`, asked of the server (`GET /api/runs/search?status=`)
 *   with the list's own search and date window, so they come first whatever
 *   page of the list is loaded. Counts are the server's `total_approx`.
 *
 * Neither claims a cause: the banner shows what was recorded and where the
 * evidence first shows a failure.
 */
import { useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { api, type FullCapsule, type RunSummary } from '../../../../lib/api';
import Button from '../../../ui/primitives/Button';
import StatusPill from '../../../ui/primitives/StatusPill';
import { endOfDay } from './useRunSearch';
import { STREAM_LABEL, firstObservedFailure, runEventsFor, runFailure } from './eventModel';

export function RunFailureBanner({
  run,
  capsule,
  onInspectEvent,
}: {
  run: RunSummary;
  capsule: FullCapsule;
  /** Open the Events view on this event key. */
  onInspectEvent?: (eventKey: string) => void;
}) {
  const failure = runFailure(run, capsule.manifest as Record<string, unknown>);
  if (!failure) return null;
  const first = firstObservedFailure(runEventsFor(capsule).events);
  return (
    <section
      aria-label="Failure reason"
      data-testid="run-failure-banner"
      className="mb-3 rounded-lg border border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[var(--color-danger-tint)] p-3 space-y-1.5 text-xs"
    >
      <h3 className="font-medium text-[var(--color-status-failure)]">
        This run did not succeed
        <span className="ml-2 font-mono font-normal text-[var(--color-text-muted)]">
          status {failure.status ?? 'not recorded'} · exit code {failure.exitCode ?? 'not recorded'}
        </span>
      </h3>
      <p className="text-[var(--color-text)]">
        {failure.error
          ? <>Recorded error: <code className="font-mono">{failure.error.type ?? 'error'}</code>{failure.error.message ? `: ${failure.error.message}` : ''}</>
          : <span className="text-[var(--color-text-muted)]">The manifest records no error detail for this run.</span>}
      </p>
      <p className="flex items-center gap-2 flex-wrap text-[var(--color-text)]">
        {first ? (
          <>
            <span>
              First observed failing event: {STREAM_LABEL[first.stream]} <code className="font-mono">{first.label}</code>
              {first.status ? <> (status <code className="font-mono">{first.status}</code>)</> : null}
              {first.error?.message ? <> — {first.error.message}</> : null}
            </span>
            {onInspectEvent && (
              <Button size="sm" variant="secondary" onClick={() => onInspectEvent(first.key)}>Inspect event</Button>
            )}
          </>
        ) : (
          <span className="text-[var(--color-text-muted)]">
            {capsule.capsule_available === false
              ? 'The capsule files are not on disk, so its calls cannot be checked.'
              : 'No recorded model or tool call has a non-success status.'}
          </span>
        )}
      </p>
      <p className="text-[10px] text-[var(--color-text-faint)]">
        &quot;First observed&quot; is the earliest call by recorded start time whose recorded status is not success — where
        the evidence first shows a failure, not a root cause.
      </p>
    </section>
  );
}

/** Statuses the strip asks the server for (the list's own status chips). */
export const FAILED_STRIP_STATUSES = ['failure', 'error'] as const;
export const FAILED_STRIP_LIMIT = 5;

interface StripState {
  loading: boolean;
  error: string | null;
  items: RunSummary[];
  totals: Record<string, number>;
}

export function FailedRunsStrip({
  search,
  since,
  until,
  refreshTick,
  selectedId,
  onOpen,
  searchRuns = api.searchRuns,
}: {
  search: string;
  since: string;
  until: string;
  refreshTick: number;
  selectedId: string | null;
  onOpen: (run: RunSummary) => void;
  searchRuns?: typeof api.searchRuns;
}) {
  const [state, setState] = useState<StripState>({ loading: true, error: null, items: [], totals: {} });

  useEffect(() => {
    let cancelled = false;
    setState(s => ({ ...s, loading: true, error: null }));
    // Inside a promise chain, so a synchronous throw lands in .catch too.
    Promise.resolve()
      .then(() => Promise.all(FAILED_STRIP_STATUSES.map(status => searchRuns({
        limit: FAILED_STRIP_LIMIT,
        status,
        q: search.trim() || undefined,
        since: since || undefined,
        until: endOfDay(until),
      }).then(r => ({ status, r })))))
      .then(pages => {
        if (cancelled) return;
        const totals: Record<string, number> = {};
        const byId = new Map<string, RunSummary>();
        for (const { status, r } of pages) {
          totals[status] = r.total_approx;
          for (const item of r.items) if (!byId.has(item.run_id)) byId.set(item.run_id, item);
        }
        const items = [...byId.values()]
          .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
          .slice(0, FAILED_STRIP_LIMIT);
        setState({ loading: false, error: null, items, totals });
      })
      .catch(e => { if (!cancelled) setState({ loading: false, error: (e as Error).message, items: [], totals: {} }); });
    return () => { cancelled = true; };
  }, [search, since, until, refreshTick, searchRuns]);

  const total = Object.values(state.totals).reduce((a, b) => a + b, 0);

  return (
    <section
      aria-label="Failed runs first"
      className="shrink-0 border-b border-[var(--color-border)] px-3 py-2 space-y-1"
    >
      <h3 className="flex items-center justify-between gap-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
        <span>Failed runs first</span>
        {!state.loading && !state.error && (
          <span className="normal-case tracking-normal font-mono" data-testid="failed-strip-totals"
            title="The server's approximate totals for status failure and status error, in this search and date window">
            {FAILED_STRIP_STATUSES.map(s => `about ${state.totals[s] ?? 0} ${s}`).join(' · ')}
          </span>
        )}
      </h3>
      {state.loading && <p role="status" className="text-[10px] text-[var(--color-text-muted)]">Looking for failed runs…</p>}
      {state.error && (
        <p role="alert" className="text-[10px] text-[var(--color-status-failure)]">Could not load failed runs: {state.error}</p>
      )}
      {!state.loading && !state.error && total === 0 && state.items.length === 0 && (
        <p className="text-[10px] text-[var(--color-text-muted)]">No run with status failure or error in this search and date window.</p>
      )}
      {state.items.length > 0 && (
        <ul className="space-y-0.5">
          {state.items.map(r => (
            <li key={r.run_id}>
              <button
                type="button"
                onClick={() => onOpen(r)}
                aria-current={selectedId === r.run_id ? 'true' : undefined}
                className={clsx(
                  'w-full flex items-center gap-2 rounded px-1.5 py-0.5 text-left text-[10px] min-w-0',
                  selectedId === r.run_id
                    ? 'bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)]'
                    : 'hover:bg-[var(--color-surface-hover)]',
                )}
              >
                <StatusPill status={r.status} variant="label" />
                <code className="font-mono truncate">{r.run_id}</code>
                <span className="ml-auto shrink-0 font-mono text-[var(--color-text-faint)]">exit {r.exit_code ?? '—'}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {!state.loading && !state.error && total > state.items.length && (
        <p className="text-[10px] text-[var(--color-text-faint)]" data-testid="failed-strip-more">
          The newest {state.items.length} shown; pick the <strong>failure</strong> or <strong>error</strong> status chip to list them all.
        </p>
      )}
      <p className="text-[10px] text-[var(--color-text-faint)]">Runs with status aborted or partial are not counted here.</p>
    </section>
  );
}
