/**
 * Run detail area: parent/child hierarchy block, view switcher
 * (Inspect / Trace / Secrets / Forensics / Children / Replay result) and the
 * per-view panes. Extracted verbatim from the former RunsTab monolith —
 * behavior frozen.
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import type { RunSummary, FullCapsule, RedactionProof, ForensicsTimeline } from '../../../../lib/api';
import CapsuleInspector from '../../../capsule/CapsuleInspector';
import TraceView from '../../../capsule/TraceView';
import ForensicsTimelinePanel from '../../../capsule/ForensicsTimelinePanel';
import { Loading } from '../../helpers';
import SecretScanPanel from './SecretScanPanel';
import { ForensicResultPane, SemanticResultPane, ExactResultPane } from './ReplayResultPanes';
import type { DetailView, ReplayResult, RunAction } from './types';

export interface SecretsState {
  runId: string;
  proof: RedactionProof | null;
  loading: boolean;
  error: string | null;
}

export interface ChildrenState {
  runId: string;
  loading: boolean;
  data: { child_count: number; children: Array<{ run_id: string; status: string | null; edge_type: string | null; exit_code: number | null }> } | null;
  error: string | null;
}

export interface ForensicsState {
  runId: string;
  loading: boolean;
  data: ForensicsTimeline | null;
  error: string | null;
}

export interface RunInspectorProps {
  selected: RunSummary | null;
  capsule: FullCapsule | null;
  detailError: string | null;
  runs: RunSummary[] | null;
  isDistributed: boolean;
  detailView: DetailView;
  setDetailView: (v: DetailView) => void;
  replayResult: { runId: string; result: ReplayResult } | null;
  secretsState: SecretsState | null;
  childrenState: ChildrenState | null;
  forensicsState: ForensicsState | null;
  onSelect: (r: RunSummary) => void;
  onAction: (run: RunSummary, action: RunAction) => void;
  onCompareTo?: (ids: string[]) => void;
}

export default function RunInspector({
  selected,
  capsule,
  detailError,
  runs,
  isDistributed,
  detailView,
  setDetailView,
  replayResult,
  secretsState,
  childrenState,
  forensicsState,
  onSelect,
  onAction,
  onCompareTo,
}: RunInspectorProps) {
  return (
    <section className="min-h-[400px] overflow-y-auto">
      {!selected && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-12 text-center h-full flex items-center justify-center">
          <p className="text-sm text-[var(--color-text-muted)]">
            Select a run · use <kbd className="font-mono text-[10px] px-1 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">j</kbd>/<kbd className="font-mono text-[10px] px-1 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">k</kbd> to navigate
          </p>
        </div>
      )}
      {selected && !capsule && !detailError && <Loading />}
      {selected && !capsule && detailError && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-8 text-center h-full flex items-center justify-center">
          <p className="text-sm text-[var(--color-text-muted)]">
            Could not load run detail: {detailError}
          </p>
        </div>
      )}
      {selected && capsule && (
        <div>
          {capsule.capsule_available === false && (
            <div className="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-[var(--color-text-muted)]">
              Capsule files are unavailable on disk for this run — showing indexed
              metadata only (status, command, timings). Sub-file sections will be empty.
            </div>
          )}
          {/* Parent/Child hierarchy (DD-1) */}
          {(() => {
            const manifest = capsule.manifest as Record<string, unknown>;
            const capsuleType = manifest.capsule_type as string | undefined;
            const parentRunId = manifest.parent_run_id as string | undefined;
            const workerRunIds = manifest.worker_run_ids as string[] | undefined;

            if (capsuleType === 'worker' && parentRunId) {
              const parentRun = runs?.find(r => r.run_id === parentRunId) ?? null;
              return (
                <div className="mb-3 px-3 py-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] flex items-center gap-2 text-xs">
                  <span className="text-[var(--color-text-faint)]">Parent run:</span>
                  <button
                    type="button"
                    onClick={() => { if (parentRun) onSelect(parentRun); }}
                    disabled={!parentRun}
                    title={parentRun ? `Jump to parent run ${parentRunId}` : 'Parent run not in current list'}
                    className={clsx(
                      'font-mono px-2 py-px rounded border transition-colors',
                      parentRun
                        ? 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] cursor-pointer'
                        : 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-default opacity-60',
                    )}
                  >
                    {parentRunId.slice(0, 16)}… ↑
                  </button>
                  <span className="text-2xs font-mono uppercase text-[var(--color-text-faint)] px-1.5 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">
                    worker
                  </span>
                </div>
              );
            }

            if (capsuleType === 'parent' && workerRunIds && workerRunIds.length > 0) {
              return (
                <ValidateDistributedBlock runId={manifest.run_id as string} workerCount={workerRunIds.length} runs={runs ?? []} workerRunIds={workerRunIds} onJump={(r) => { const found = runs?.find(x => x.run_id === r); if (found) onSelect(found); }} />
              );
            }

            return null;
          })()}

          <header className="mb-3 flex items-center justify-between flex-wrap gap-3">
            <div>
              <code className="font-mono text-sm text-[var(--color-text)] break-all">{capsule.run_id}</code>
              <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono break-all">{capsule.capsule_path}</p>
            </div>
            {/* View switcher */}
            <div className="inline-flex rounded-md border border-[var(--color-border)] overflow-hidden text-xs">
              {(['inspect', 'trace'] as DetailView[]).map(v => (
                <button
                  key={v}
                  onClick={() => setDetailView(v)}
                  className={[
                    'px-3 py-1 capitalize font-medium transition-colors',
                    detailView === v
                      ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                      : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]',
                  ].join(' ')}
                >
                  {v === 'trace' ? 'Trace' : 'Inspect'}
                </button>
              ))}
              <button
                onClick={() => setDetailView('secrets')}
                className={[
                  'px-3 py-1 font-medium transition-colors border-l border-[var(--color-border)]',
                  detailView === 'secrets'
                    ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                    : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]',
                ].join(' ')}
              >
                Secrets
              </button>
              <button
                onClick={() => setDetailView('forensics')}
                className={[
                  'px-3 py-1 font-medium transition-colors border-l border-[var(--color-border)]',
                  detailView === 'forensics'
                    ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                    : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]',
                ].join(' ')}
              >
                Forensics
              </button>
              {isDistributed && (
                <button
                  onClick={() => setDetailView('children')}
                  className={[
                    'px-3 py-1 font-medium transition-colors border-l border-[var(--color-border)]',
                    detailView === 'children'
                      ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                      : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]',
                  ].join(' ')}
                >
                  Children
                </button>
              )}
              {replayResult?.runId === selected.run_id && (
                <button
                  onClick={() => setDetailView('replay')}
                  className={[
                    'px-3 py-1 font-medium transition-colors border-l border-[var(--color-border)]',
                    detailView === 'replay'
                      ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                      : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]',
                  ].join(' ')}
                >
                  Replay result
                </button>
              )}
            </div>
          </header>
          {detailView === 'inspect' && (
            <CapsuleInspector
              capsuleData={{
                capsule: capsule.manifest as Record<string, unknown>,
                capsuleYaml: '',
                modelCalls: capsule.model_calls,
                toolCalls: capsule.tool_calls,
                trace: capsule.trace,
                inputs: capsule.inputs ?? [],
                outputs: capsule.outputs ?? [],
              }}
              onCompareTo={onCompareTo ? (rA: string, rB: string) => onCompareTo([rA, rB]) : undefined}
            />
          )}
          {detailView === 'trace' && (
            <TraceView
              trace={capsule.trace}
              modelCalls={capsule.model_calls}
              toolCalls={capsule.tool_calls}
            />
          )}
          {detailView === 'secrets' && (() => {
            const sel = selected;
            return (
              <SecretScanPanel
                runId={sel.run_id}
                capsulePath={capsule.capsule_path}
                proof={secretsState?.runId === sel.run_id ? (secretsState.proof ?? null) : null}
                loading={secretsState?.runId === sel.run_id ? secretsState.loading : true}
                error={secretsState?.runId === sel.run_id ? secretsState.error : null}
                onRunRedact={() => onAction(sel, 'redact')}
              />
            );
          })()}
          {detailView === 'forensics' && (() => {
            const fs = forensicsState?.runId === selected.run_id ? forensicsState : null;
            return (
              <div className="p-4">
                <ForensicsTimelinePanel
                  data={fs?.data ?? null}
                  loading={fs ? fs.loading : true}
                  error={fs?.error ?? null}
                />
              </div>
            );
          })()}
          {detailView === 'children' && (() => {
            const cs = childrenState?.runId === selected.run_id ? childrenState : null;
            if (!cs || cs.loading) {
              return <div className="p-4 text-xs text-[var(--color-text-faint)]">Loading children…</div>;
            }
            if (cs.error) {
              return <div className="p-4 text-xs text-[var(--color-status-failure)] font-mono">{cs.error}</div>;
            }
            if (!cs.data || cs.data.child_count === 0) {
              return (
                <div className="p-4 text-xs text-[var(--color-text-faint)]">
                  No child runs. Use <code className="font-mono">nova run show --with-children</code> from CLI for distributed runs.
                </div>
              );
            }
            return (
              <div className="p-4 space-y-2">
                <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
                  {cs.data.child_count} child run{cs.data.child_count !== 1 ? 's' : ''}
                </p>
                <div className="space-y-1">
                  {cs.data.children.map((child) => (
                    <div
                      key={child.run_id}
                      className="flex items-center gap-3 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-1.5 font-mono"
                    >
                      <span className="flex-1 text-[var(--color-text)] truncate">{child.run_id}</span>
                      {child.edge_type && (
                        <span className="text-2xs px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
                          {child.edge_type}
                        </span>
                      )}
                      {child.status && (
                        <span className={clsx(
                          'text-2xs px-1.5 py-0.5 rounded',
                          child.status === 'COMPLETED' ? 'text-[var(--color-status-success)]'
                            : child.status === 'FAILED' ? 'text-[var(--color-status-failure)]'
                            : 'text-[var(--color-status-pending)]',
                        )}>
                          {child.status}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}
          {detailView === 'replay' && replayResult?.runId === selected.run_id && (
            replayResult.result.mode === 'semantic'
              ? <SemanticResultPane result={replayResult.result} />
              : replayResult.result.mode === 'exact'
              ? <ExactResultPane result={replayResult.result} />
              : <ForensicResultPane result={replayResult.result} />
          )}
        </div>
      )}
    </section>
  );
}

function ValidateDistributedBlock({
  runId,
  workerCount,
  runs,
  workerRunIds,
  onJump,
}: {
  runId: string;
  workerCount: number;
  runs: RunSummary[];
  workerRunIds: string[];
  onJump: (runId: string) => void;
}) {
  const [result, setResult] = useState<{ ok: boolean; status: string; message: string; exit_code: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function runValidate() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.validateDistributed(runId);
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs">
          <span className="text-2xs font-mono uppercase text-[var(--color-accent)] px-1.5 py-px rounded bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] border border-[color-mix(in_oklab,var(--color-accent)_30%,transparent)]">
            parent
          </span>
          <span className="text-[var(--color-text-faint)]">{workerCount} worker{workerCount !== 1 ? 's' : ''}</span>
        </div>
        <button
          type="button"
          onClick={runValidate}
          disabled={busy}
          className="px-2.5 py-1 text-[10px] font-mono rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] disabled:opacity-50 transition-colors"
        >
          {busy ? 'Validating…' : 'Validate distributed run'}
        </button>
      </div>

      {/* Worker list */}
      <div className="flex flex-wrap gap-1.5">
        {workerRunIds.map(wid => {
          const found = runs.find(r => r.run_id === wid);
          return (
            <button
              key={wid}
              type="button"
              onClick={() => onJump(wid)}
              disabled={!found}
              title={found ? `Jump to worker ${wid}` : 'Worker not in current list'}
              className={clsx(
                'font-mono text-2xs px-2 py-px rounded border transition-colors',
                found
                  ? 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] cursor-pointer'
                  : 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-default opacity-50',
              )}
            >
              {wid.slice(0, 12)}… ↓
            </button>
          );
        })}
      </div>

      {err && <p className="text-xs text-[var(--color-status-failure)]">{err}</p>}
      {result && (
        <div className={clsx(
          'rounded border px-3 py-2 text-xs font-mono',
          result.ok
            ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)] text-[var(--color-status-success)]'
            : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)] text-[var(--color-status-failure)]',
        )}>
          <span className="mr-2">{result.ok ? '✓' : '✗'}</span>
          {result.message}
          {result.status && result.status !== 'unknown' && (
            <span className="ml-2 text-2xs opacity-70">[{result.status}]</span>
          )}
        </div>
      )}

      <p className="text-2xs font-mono text-[var(--color-text-faint)]">
        CLI: <code>nova validate-distributed {runId}</code>
      </p>
    </div>
  );
}
