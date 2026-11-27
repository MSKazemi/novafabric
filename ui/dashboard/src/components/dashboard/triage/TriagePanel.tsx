/**
 * Triage panel (issue #276, experimental): from one alert or incident, the
 * exact run(s) it links to, its verification state, a timeline in words, and
 * the next safe actions as copyable CLI commands.
 *
 * Read-only by construction: the only requests are `GET /api/runs/{id}` for
 * the runs the record itself names (so the server's own authorization decides
 * what is shown), and no command is executed from here. When a run cannot be
 * loaded the panel says so with the server's message — it never fills in a
 * status or a path it did not receive.
 */
import { useEffect, useId, useState } from 'react';
import { api, ServeApiError, type FullCapsule } from '../../../lib/api';
import Drawer from '../../ui/primitives/Drawer';
import Badge from '../../ui/primitives/Badge';
import CopyButton from '../../ui/CopyButton';
import { renderCommand, type TriageModel } from './triageModel';

/** At most this many linked runs are loaded; the rest are listed by id only. */
export const MAX_LOADED_RUNS = 5;

type RunState =
  | { state: 'loading' }
  | {
      state: 'ok';
      status: string | null;
      createdAt: string | null;
      capsulePath: string | null;
      capsuleAvailable: boolean;
    }
  | { state: 'error'; message: string; httpStatus: number | null };

export interface TriagePanelProps {
  model: TriageModel;
  onClose: () => void;
  /** Injected for tests; defaults to the dashboard API client. */
  loadRun?: (runId: string) => Promise<FullCapsule>;
}

/** Same-page deep link to one run in the Runs tab. A full navigation, so Back returns here. */
export function runHref(runId: string): string {
  const p = new URLSearchParams({ tab: 'runs', run: runId });
  return `${typeof window === 'undefined' ? '' : window.location.pathname}?${p.toString()}`;
}

/** Same-page deep link that opens the Diff tab on these runs. */
export function diffHref(runIds: string[]): string {
  const p = new URLSearchParams({ run_ids: runIds.join(',') });
  return `${typeof window === 'undefined' ? '' : window.location.pathname}?${p.toString()}`;
}

/** Deterministic, timezone-explicit rendering of an ISO time (UTC). */
export function formatAt(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Date(ms).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC');
}

function summarize(run: FullCapsule): RunState {
  const m = (run.manifest ?? {}) as Record<string, unknown>;
  return {
    state: 'ok',
    status: typeof m.status === 'string' ? m.status : null,
    createdAt: typeof m.created_at === 'string' ? m.created_at : null,
    capsulePath: typeof run.capsule_path === 'string' && run.capsule_path ? run.capsule_path : null,
    capsuleAvailable: run.capsule_available !== false,
  };
}

const sectionTitle = 'text-2xs font-mono uppercase tracking-wider text-[var(--color-text-faint)] mb-2';
const linkClass = 'text-[var(--color-accent)] underline underline-offset-2 hover:no-underline focus-visible:outline focus-visible:outline-2';

export default function TriagePanel({ model, onClose, loadRun = api.getRun }: TriagePanelProps) {
  const ids = {
    runs: useId(),
    verify: useId(),
    timeline: useId(),
    actions: useId(),
  };
  const loaded = model.runIds.slice(0, MAX_LOADED_RUNS);
  const notLoaded = model.runIds.slice(MAX_LOADED_RUNS);
  const [runs, setRuns] = useState<Record<string, RunState>>(() =>
    Object.fromEntries(loaded.map((id) => [id, { state: 'loading' } as RunState])),
  );

  const runKey = loaded.join('\n');
  useEffect(() => {
    let live = true;
    setRuns(Object.fromEntries(loaded.map((id) => [id, { state: 'loading' } as RunState])));
    for (const id of loaded) {
      loadRun(id)
        .then((run) => { if (live) setRuns((prev) => ({ ...prev, [id]: summarize(run) })); })
        .catch((e: unknown) => {
          if (!live) return;
          const httpStatus = e instanceof ServeApiError ? e.status : null;
          const message = e instanceof Error ? e.message : String(e);
          setRuns((prev) => ({ ...prev, [id]: { state: 'error', message, httpStatus } }));
        });
    }
    return () => { live = false; };
    // runKey captures `loaded`; loadRun is stable for a given caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runKey]);

  const capsuleDirFor = (runId: string | undefined): string | null => {
    if (!runId) return null;
    const s = runs[runId];
    return s && s.state === 'ok' ? s.capsulePath : null;
  };

  return (
    <Drawer title={`Triage — ${model.title}`} onClose={onClose} widthClass="max-w-2xl">
      <div className="space-y-6 text-xs">
        <p className="text-[var(--color-text-muted)]">
          {model.kind === 'alert' ? 'Alert' : 'Incident'}{' '}
          <code className="font-mono text-[var(--color-text)]">{model.recordId}</code>. Read-only:
          nothing on this panel runs a command or changes a record.
        </p>

        <section aria-labelledby={ids.runs}>
          <h4 id={ids.runs} className={sectionTitle}>Linked runs</h4>
          {model.failedCapsule && (
            <dl className="mb-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[var(--color-text-muted)]">
              <dt>failed capsule</dt>
              <dd className="font-mono break-all text-[var(--color-text)]">
                {model.failedCapsule.dir ?? 'directory not recorded (alert predates this field)'}
              </dd>
              <dt>seal capsule id</dt>
              <dd className="font-mono break-all">{model.failedCapsule.capsuleId ?? 'not recorded'}</dd>
            </dl>
          )}
          {model.claimedRun && (
            <div role="note" aria-label="Unverified run claim" className="mb-2 rounded border border-[color-mix(in_oklab,var(--color-status-pending)_45%,transparent)] bg-[var(--color-pending-tint)] px-3 py-2 space-y-1">
              <p className="text-[var(--color-text)]">
                <Badge tone="pending">unverified</Badge>{' '}
                Claimed run <code className="font-mono">{model.claimedRun.runId}</code>
              </p>
              <p className="text-[var(--color-text-muted)]">{model.claimedRun.caveat}</p>
              <p>
                <a
                  href={runHref(model.claimedRun.runId)}
                  className={linkClass}
                  aria-label={`Open claimed run ${model.claimedRun.runId} (unverified) in the Runs tab`}
                >
                  Open claimed run (unverified)
                </a>
              </p>
            </div>
          )}
          {model.unsafeRunIds.length > 0 && (
            <p role="note" className="mb-2 text-[var(--color-status-failure)]">
              Not a plain run id, so not loaded or linked:{' '}
              {model.unsafeRunIds.map((id, i) => (
                <span key={id}>{i > 0 && ', '}<code className="font-mono">{JSON.stringify(id)}</code></span>
              ))}
            </p>
          )}
          {model.runIds.length === 0 ? (model.noRunReason ? (
            <div role="note" className="rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-3 py-2 space-y-1">
              <p className="font-semibold text-[var(--color-text)]">{model.noRunReason}</p>
              {model.noRunHint && <p className="text-[var(--color-text-muted)]">{model.noRunHint}</p>}
            </div>
          ) : null) : (
            <ul className="space-y-2">
              {loaded.map((id) => {
                const s = runs[id] ?? { state: 'loading' };
                return (
                  <li key={id} className="rounded border border-[var(--color-border)] px-3 py-2 space-y-1" aria-busy={s.state === 'loading'}>
                    <div className="flex items-center justify-between gap-2">
                      <code className="font-mono text-[var(--color-text)] truncate">{id}</code>
                      <a href={runHref(id)} className={linkClass} aria-label={`Open run ${id} in the Runs tab`}>
                        Open run
                      </a>
                    </div>
                    {s.state === 'loading' && <p className="text-[var(--color-text-faint)]" aria-live="polite">Loading run…</p>}
                    {s.state === 'error' && (
                      <p role="status" className="text-[var(--color-status-failure)]">
                        Could not load this run{s.httpStatus ? ` (HTTP ${s.httpStatus})` : ''}: {s.message}. Nothing is shown
                        in its place — it may have been deleted, or this credential may not be allowed to read it.
                      </p>
                    )}
                    {s.state === 'ok' && (
                      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[var(--color-text-muted)]">
                        <dt>status</dt>
                        <dd className="font-mono">{s.status ?? 'not recorded'}</dd>
                        <dt>created</dt>
                        <dd className="font-mono">{s.createdAt ? formatAt(s.createdAt) : 'not recorded'}</dd>
                        <dt>capsule</dt>
                        <dd className="font-mono break-all">
                          {s.capsulePath ?? 'no path recorded'}
                          {!s.capsuleAvailable && (
                            <span className="block text-[var(--color-status-pending)]">
                              The capsule directory is missing on disk — only indexed metadata is available.
                            </span>
                          )}
                        </dd>
                      </dl>
                    )}
                  </li>
                );
              })}
              {notLoaded.length > 0 && (
                <li className="text-[var(--color-text-muted)]">
                  {notLoaded.length} more linked run{notLoaded.length === 1 ? '' : 's'} not loaded here:{' '}
                  {notLoaded.map((id, i) => (
                    <span key={id}>
                      {i > 0 && ', '}
                      <a href={runHref(id)} className={linkClass}>{id}</a>
                    </span>
                  ))}
                </li>
              )}
            </ul>
          )}
          {model.runIds.length >= 2 && (
            <p className="mt-2">
              <a href={diffHref(model.runIds.slice(0, 2))} className={linkClass}>
                Compare {model.runIds[0]} and {model.runIds[1]} in Diff
              </a>
            </p>
          )}
        </section>

        <section aria-labelledby={ids.verify}>
          <h4 id={ids.verify} className={sectionTitle}>Verification</h4>
          {model.verify.kind === 'failed-recorded' ? (
            <p className="text-[var(--color-text-muted)]">
              <Badge tone="danger" dot>seal failed</Badge>{' '}
              Recorded by <code className="font-mono">{model.verify.source}</code> at{' '}
              <time dateTime={model.verify.at}>{formatAt(model.verify.at)}</time>. This panel did not re-run the
              check; the verify command below re-checks it now.
            </p>
          ) : (
            <p className="text-[var(--color-text-muted)]">
              <Badge tone="neutral">not checked here</Badge>{' '}
              This panel does not run verification. Use the verify command below
              {model.runIds.length ? '' : ' once a run is known'}.
            </p>
          )}
          {model.firstDivergence ? (
            <p className="mt-2 text-[var(--color-text-muted)]">
              First divergence:{' '}
              <code className="font-mono text-[var(--color-text)] break-all">{model.firstDivergence.path}</code>{' '}
              ({model.firstDivergence.reason || 'changed'}
              {model.firstDivergence.divergent_files !== null && model.firstDivergence.divergent_files > 1
                ? `, ${model.firstDivergence.divergent_files} files diverge`
                : ''}
              ) — file granularity; no record inside the file is named.
            </p>
          ) : model.verify.kind === 'failed-recorded' ? (
            <p className="mt-2 text-[var(--color-text-muted)]">
              No first divergence was recorded with this alert (the seal may predate per-file digests, the
              failure may be in the signature or log rather than a file, or the alert predates this field).
            </p>
          ) : model.runIds.length >= 2 ? (
            <p className="mt-2 text-[var(--color-text-muted)]">
              No diff is stored for these runs. Open them in Diff, or run the diff command below.
            </p>
          ) : null}
        </section>

        <section aria-labelledby={ids.timeline}>
          <h4 id={ids.timeline} className={sectionTitle}>What happened</h4>
          <ol className="space-y-1.5">
            {model.timeline.map((t, i) => (
              <li key={i} className="grid grid-cols-[11rem_1fr] gap-2">
                {t.at ? (
                  <time dateTime={t.at} className="font-mono text-[var(--color-text-faint)]">{formatAt(t.at)}</time>
                ) : (
                  <span className="font-mono text-[var(--color-text-faint)]">no time recorded</span>
                )}
                <span className="text-[var(--color-text)]">{t.text}</span>
              </li>
            ))}
          </ol>
        </section>

        <section aria-labelledby={ids.actions}>
          <h4 id={ids.actions} className={sectionTitle}>Next safe actions</h4>
          <p className="mb-2 text-[var(--color-text-muted)]">
            Copy and run these in a terminal on the host that holds the capsules. Nothing here runs from the browser.
          </p>
          <ul className="space-y-2">
            {model.commands.map((c) => {
              const line = renderCommand(c, capsuleDirFor(c.needsCapsuleDir));
              return (
                <li key={c.template} className="rounded border border-[var(--color-border)] px-3 py-2 space-y-1">
                  <div className="flex items-start justify-between gap-2">
                    <code className="font-mono text-[var(--color-text)] break-all">{line}</code>
                    <CopyButton text={line} label="Copy" />
                  </div>
                  <p className="text-[var(--color-text-muted)]">{c.purpose}</p>
                  {line.includes('<capsule-dir-of-') && (
                    <p className="text-[var(--color-text-faint)]">
                      The capsule directory appears here once the run loads; until then the placeholder must be replaced.
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      </div>
    </Drawer>
  );
}
