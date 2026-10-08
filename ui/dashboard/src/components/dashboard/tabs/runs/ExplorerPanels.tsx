/**
 * Capsule explorer panels for the Runs inspector:
 *
 * - `CapsuleSummary`  — the at-a-glance strip (status, timing, call counts) and
 *                       the run's primary actions, so the explorer is
 *                       actionable without hunting through the list row.
 * - `IntegrityPanel`  — seal verification (`nova verify`, POST /api/runs/{id}/verify).
 *                       "Not sealed" and "verifier not configured" are stated as
 *                       what they are — never rendered as a pass or a failure.
 * - `LineageNeighbours` — the run's spool lineage edges (`nova run lineage`) as
 *                       navigable neighbours, each with a Compare action.
 */
import { useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { api, type CapsuleVerifyResult, type FullCapsule, type RunSummary } from '../../../../lib/api';
import StatusPill from '../../../ui/primitives/StatusPill';
import Button from '../../../ui/primitives/Button';
import CopyButton from '../../../ui/CopyButton';
import EmptyState from '../../../ui/EmptyState';
import { ErrorBox, Loading } from '../../helpers';
import type { RunAction } from './types';

function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  return s < 120 ? `${s.toFixed(1)} s` : `${(s / 60).toFixed(1)} min`;
}

function Fact({ label, value, mono = true }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs uppercase tracking-wider text-[var(--color-text-faint)]">{label}</dt>
      <dd className={clsx('text-xs text-[var(--color-text)] truncate', mono && 'font-mono tabular-nums')}>{value}</dd>
    </div>
  );
}

export function CapsuleSummary({
  run,
  capsule,
  onAction,
}: {
  run: RunSummary;
  capsule: FullCapsule;
  onAction: (run: RunSummary, action: RunAction) => void;
}) {
  const m = capsule.manifest as Record<string, unknown>;
  const pick = <T,>(key: keyof RunSummary & string): T | null =>
    ((run[key] ?? m[key]) as T | null | undefined) ?? null;
  const status = pick<string>('status');
  const created = pick<string>('created_at');
  const duration = pick<number>('duration_ms');
  const exit = pick<number>('exit_code');
  const modelCalls = capsule.model_calls?.length ?? pick<number>('model_call_count') ?? 0;
  const toolCalls = capsule.tool_calls?.length ?? pick<number>('tool_call_count') ?? 0;
  const mutating = pick<number>('mutating_tool_count') ?? 0;
  const command = (run.command?.length ? run.command : (m.command as string[] | undefined)) ?? [];
  const link = typeof window === 'undefined' ? '' : window.location.href;

  return (
    <section
      aria-label="Run summary"
      className="mb-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 space-y-3"
    >
      <dl className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-3">
        <Fact label="Status" value={<StatusPill status={status} variant="label" />} mono={false} />
        <Fact label="Started" value={created ? new Date(created).toLocaleString() : '—'} />
        <Fact label="Duration" value={fmtDuration(duration)} />
        <Fact label="Exit code" value={exit ?? '—'} />
        <Fact label="Model calls" value={modelCalls} />
        <Fact label="Tool calls" value={mutating > 0 ? `${toolCalls} (${mutating} mutating)` : toolCalls} />
      </dl>
      {command.length > 0 && (
        <code className="block text-[10px] font-mono text-[var(--color-text-muted)] truncate" title={command.join(' ')}>
          $ {command.join(' ')}
        </code>
      )}
      <div className="flex items-center gap-1.5 flex-wrap" role="group" aria-label="Run actions">
        <Button size="sm" variant="secondary" onClick={() => onAction(run, 'dry-run')}>Replay dry-run</Button>
        <Button size="sm" variant="secondary" onClick={() => onAction(run, 'replay')}>Forensic replay</Button>
        <Button size="sm" variant="secondary" onClick={() => onAction(run, 'export')}>Export evidence</Button>
        {link && <CopyButton text={link} label="Copy link" />}
      </div>
    </section>
  );
}

type Check = { label: string; ok: boolean | undefined };

export function IntegrityPanel({
  runId,
  verify = api.capsuleVerify,
}: {
  runId: string;
  verify?: (runId: string) => Promise<CapsuleVerifyResult>;
}) {
  const [state, setState] = useState<{ busy: boolean; result: CapsuleVerifyResult | null; error: string | null }>({
    busy: false, result: null, error: null,
  });

  // A different run is a different verification — never show the last one's verdict.
  useEffect(() => { setState({ busy: false, result: null, error: null }); }, [runId]);

  async function run() {
    setState({ busy: true, result: null, error: null });
    try {
      setState({ busy: false, result: await verify(runId), error: null });
    } catch (e) {
      setState({ busy: false, result: null, error: (e as Error).message });
    }
  }

  const r = state.result;
  const checks: Check[] = r && r.sealed && r.configured
    ? [
        { label: 'DSSE signature', ok: r.signature_ok },
        { label: 'RFC 3161 timestamp', ok: r.timestamp_ok },
        { label: 'Merkle log inclusion', ok: r.log_integrity_ok },
      ]
    : [];

  return (
    <section aria-label="Seal verification" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-xs font-medium text-[var(--color-text)]">Seal verification</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
            nova verify — signature, timestamp and transparency-log inclusion
          </p>
        </div>
        <Button size="sm" onClick={run} pending={state.busy}>
          {r ? 'Verify again' : 'Verify seal'}
        </Button>
      </header>
      <div aria-live="polite">
        {state.busy && <Loading />}
        {state.error && <ErrorBox message={state.error} onRetry={run} />}
        {!state.busy && !state.error && !r && (
          <p className="text-xs text-[var(--color-text-muted)]">Not verified in this session.</p>
        )}
        {r && !r.sealed && (
          <p className="text-xs text-[var(--color-text-muted)]" data-testid="integrity-verdict">
            <strong>Not sealed.</strong> {r.message ?? 'This capsule carries no seal, so there is nothing to verify.'}
          </p>
        )}
        {r && r.sealed && r.configured === false && (
          <p className="text-xs text-[var(--color-text-muted)]" data-testid="integrity-verdict">
            <strong>Sealed, but not verifiable here.</strong> {r.message}
          </p>
        )}
        {checks.length > 0 && (
          <div className="space-y-2">
            <p
              data-testid="integrity-verdict"
              className={clsx('text-xs font-medium', r?.valid ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}
            >
              {r?.valid ? 'Seal verified' : 'Seal verification FAILED'}
            </p>
            <ul className="space-y-1">
              {checks.map(c => (
                <li key={c.label} className="flex items-center gap-2 text-xs">
                  <StatusPill status={c.ok ? 'passed' : 'failed'} variant="dot" />
                  <span>{c.label}</span>
                  <span className="text-[var(--color-text-faint)]">{c.ok ? 'ok' : 'failed'}</span>
                </li>
              ))}
            </ul>
            {r?.errors && r.errors.length > 0 && (
              <ul className="list-disc pl-5 text-[10px] text-[var(--color-status-failure)]">
                {r.errors.map(e => <li key={e}>{e}</li>)}
              </ul>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

interface Edge { source_run_id?: string; target_run_id?: string; edge_type?: string }

export function LineageNeighbours({
  runId,
  onOpen,
  onCompareTo,
  load = api.runSpoolLineage,
}: {
  runId: string;
  onOpen: (runId: string) => void;
  onCompareTo?: (ids: string[]) => void;
  load?: (runId: string) => Promise<{ edges: Array<Record<string, unknown>>; count: number }>;
}) {
  const [state, setState] = useState<{ loading: boolean; edges: Edge[] | null; error: string | null }>({
    loading: true, edges: null, error: null,
  });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, edges: null, error: null });
    load(runId)
      .then(r => { if (!cancelled) setState({ loading: false, edges: r.edges as Edge[], error: null }); })
      .catch(e => { if (!cancelled) setState({ loading: false, edges: null, error: (e as Error).message }); });
    return () => { cancelled = true; };
  }, [runId, load, tick]);

  return (
    <section aria-label="Lineage neighbours" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header>
        <h3 className="text-xs font-medium text-[var(--color-text)]">Lineage neighbours</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
          nova run lineage {runId}
        </p>
      </header>
      {state.loading && <Loading />}
      {state.error && <ErrorBox message={state.error} onRetry={() => setTick(t => t + 1)} />}
      {state.edges && state.edges.length === 0 && (
        <EmptyState
          variant="inline"
          message="No lineage edges recorded for this run."
          hint="Edges are written by distributed (parent/worker) capsules and spool lineage."
        />
      )}
      {state.edges && state.edges.length > 0 && (
        <ul className="divide-y divide-[var(--color-border)]">
          {state.edges.map((e, i) => {
            const outgoing = e.source_run_id === runId;
            const other = (outgoing ? e.target_run_id : e.source_run_id) ?? '';
            return (
              <li key={`${other}-${e.edge_type}-${i}`} className="flex items-center gap-2 py-1.5 text-xs min-w-0">
                <span className="shrink-0 text-2xs font-mono uppercase text-[var(--color-text-faint)] w-16">
                  {outgoing ? 'to →' : '← from'}
                </span>
                <span className="shrink-0 text-2xs font-mono px-1.5 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">
                  {e.edge_type ?? 'contains'}
                </span>
                <button
                  type="button"
                  disabled={!other}
                  onClick={() => onOpen(other)}
                  className="font-mono truncate text-[var(--color-accent)] hover:underline disabled:text-[var(--color-text-faint)] disabled:no-underline"
                  title={`Open ${other}`}
                >
                  {other || '(unknown run)'}
                </button>
                {onCompareTo && other && (
                  <Button size="sm" variant="ghost" className="ml-auto shrink-0" onClick={() => onCompareTo([runId, other])}>
                    Compare
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
