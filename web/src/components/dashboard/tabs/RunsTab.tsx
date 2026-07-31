/**
 * Runs tab — orchestrating shell over the `./runs/` split.
 *
 * The list, filters, inspector, replay panes, secret-scan panel, and parity
 * panels live in `./runs/` (one cohesive unit each); this file owns the
 * cross-cutting state (selection, detail fetches, action confirm flow) and
 * composition. Extracted verbatim from the former 2.1K-line monolith —
 * behavior frozen; same default export + props.
 */
import { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from '../../../lib/api';
import type { RunSummary, FullCapsule } from '../../../lib/api';
import ConfirmDialog from '../ConfirmDialog';
import { ErrorBox, Loading } from '../helpers';
import type { Tab } from '../Sidebar';
import type { DetailView, RunAction, RunSort, StatusFilter, ValidationState, ReplayResult } from './runs/types';
import { buildActionMeta } from './runs/actionMeta';
import { useRunSearch } from './runs/useRunSearch';
import RunFilters from './runs/RunFilters';
import RunList from './runs/RunList';
import RunInspector, { type SecretsState, type ChildrenState, type ForensicsState } from './runs/RunInspector';
import { CapsuleTreePanel, RunSpoolLineagePanel, ScanSecretsPanel } from './runs/ParityPanels';

export default function RunsTab({
  onFlash,
  refreshTick,
  onCountChange,
  onNavigate,
  onCompareTo,
}: {
  onFlash: (tone: 'success' | 'error', text: string) => void;
  refreshTick: number;
  onCountChange?: (n: number) => void;
  onNavigate?: (tab: Tab) => void;
  onCompareTo?: (ids: string[]) => void;
}) {
  const [selected, setSelected] = useState<RunSummary | null>(null);
  const [capsule, setCapsule] = useState<FullCapsule | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionTarget, setActionTarget] = useState<{ run: RunSummary; action: RunAction } | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [sort, setSort] = useState<RunSort>('newest');
  const [detailView, setDetailView] = useState<DetailView>('inspect');
  const [replayResult, setReplayResult] = useState<{ runId: string; result: ReplayResult } | null>(null);
  const [since, setSince] = useState('');
  const [until, setUntil] = useState('');

  // A run can only have children if it's a distributed (parent/worker) capsule.
  // For ordinary single-process runs the "Children" tab is meaningless and
  // always empty, so we hide it rather than show a perpetual "No child runs".
  const isDistributed = useMemo(() => {
    if (!capsule) return false;
    const m = capsule.manifest as Record<string, unknown>;
    const workers = m.worker_run_ids;
    return (
      m.capsule_type === 'parent' ||
      m.capsule_type === 'worker' ||
      !!m.parent_run_id ||
      (Array.isArray(workers) && workers.length > 0)
    );
  }, [capsule]);
  const [compareA, setCompareA] = useState<string | null>(null);
  // Multi-select state for compare shortcut (at most 2 runs)
  const [checkedIds, setCheckedIds] = useState<string[]>([]);
  const [secretsState, setSecretsState] = useState<SecretsState | null>(null);
  const [secretsReloadTick, setSecretsReloadTick] = useState(0);
  const [childrenState, setChildrenState] = useState<ChildrenState | null>(null);
  const [forensicsState, setForensicsState] = useState<ForensicsState | null>(null);
  const [validationStates, setValidationStates] = useState<Record<string, ValidationState>>({});

  // Cursor search + load-more (B-1), SSE live prepend (B-3), cost summary.
  const {
    runs, setRuns, error, totalApprox, hasMore, loadingMore, liveConnected,
    costMap, refresh, loadMore,
  } = useRunSearch({ search, statusFilter, since, until, refreshTick, onCountChange });

  useEffect(() => {
    if (!selected) { setCapsule(null); setDetailError(null); return; }
    setCapsule(null);
    setDetailError(null);
    api.getRun(selected.run_id)
      .then(setCapsule)
      .catch(e => setDetailError((e as Error).message));
  }, [selected]);

  // Keyboard: j/k to move selection in the run list
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (!runs || runs.length === 0) return;
      if (e.key === 'j' || e.key === 'k') {
        const visible = visibleRuns;
        if (visible.length === 0) return;
        const idx = selected ? visible.findIndex(r => r.run_id === selected.run_id) : -1;
        const next = e.key === 'j' ? Math.min(idx + 1, visible.length - 1) : Math.max(idx - 1, 0);
        setSelected(visible[next]);
      }
      if (e.key === 'Escape') setSelected(null);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  useEffect(() => {
    if (detailView !== 'secrets' || !selected) return;
    const runId = selected.run_id;
    if (secretsState?.runId === runId && !secretsState.loading) return;
    setSecretsState({ runId, proof: null, loading: true, error: null });
    api.getRedactionProof(runId)
      .then(proof => setSecretsState({ runId, proof, loading: false, error: null }))
      .catch(e => setSecretsState({ runId, proof: null, loading: false, error: (e as Error).message }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailView, selected?.run_id, secretsReloadTick]);

  useEffect(() => {
    if (detailView !== 'children' || !selected) return;
    const runId = selected.run_id;
    if (childrenState?.runId === runId && !childrenState.loading) return;
    setChildrenState({ runId, loading: true, data: null, error: null });
    api.getRunChildren(runId)
      .then(r => setChildrenState({ runId, loading: false, data: r, error: null }))
      .catch(e => setChildrenState({ runId, loading: false, data: null, error: (e as Error).message }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailView, selected?.run_id]);

  useEffect(() => {
    if (detailView !== 'forensics' || !selected) return;
    const runId = selected.run_id;
    if (forensicsState?.runId === runId && !forensicsState.loading) return;
    setForensicsState({ runId, loading: true, data: null, error: null });
    api.getRunForensicsTimeline(runId)
      .then(data => setForensicsState({ runId, loading: false, data, error: null }))
      .catch(e => setForensicsState({ runId, loading: false, data: null, error: (e as Error).message }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailView, selected?.run_id]);

  // If the view is stuck on "children" but the newly-selected run isn't a
  // distributed capsule (so the Children tab is hidden), fall back to Inspect.
  useEffect(() => {
    if (detailView === 'children' && capsule && !isDistributed) setDetailView('inspect');
  }, [detailView, capsule, isDistributed]);

  const onActionConfirm = useCallback(async () => {
    if (!actionTarget) return;
    const { run, action } = actionTarget;
    setActionBusy(true);
    try {
      if (action === 'export') {
        const res = await api.exportEvidence(run.run_id);
        const note = res.key_autogenerated ? ' (signing key auto-generated)' : '';
        onFlash('success', `Bundle written: ${res.bundle_path} (${res.size_bytes} bytes)${note}`);
      } else if (action === 'replay') {
        const res = await api.forensicReplay(run.run_id);
        const r = res.result as unknown as ReplayResult;
        setReplayResult({ runId: run.run_id, result: r });
        setSelected(run);
        setDetailView('replay');
        onFlash('success', `Forensic replay ${r.status} (id: ${r.replay_id.slice(0, 10)}…)`);
      } else if (action === 'dry-run') {
        const res = await api.dryRunReplay(run.run_id);
        const r = res.result as unknown as ReplayResult;
        setReplayResult({ runId: run.run_id, result: r });
        setSelected(run);
        setDetailView('replay');
        const blocked = r.dry_run_report ? (r.dry_run_report.match(/BLOCK/g) ?? []).length : 0;
        onFlash(blocked > 0 ? 'error' : 'success', `Dry-run: ${blocked} tool(s) would be blocked`);
      } else if (action === 'semantic') {
        const res = await api.semanticReplay(run.run_id);
        const r = res.result as unknown as ReplayResult;
        setReplayResult({ runId: run.run_id, result: r });
        setSelected(run);
        setDetailView('replay');
        const score = r.similarity_score != null ? `${(r.similarity_score * 100).toFixed(1)}%` : '—';
        onFlash('success', `Semantic analysis complete — similarity: ${score}`);
      } else if (action === 'exact') {
        const res = await api.exactReplay(run.run_id);
        const r = res.result as unknown as ReplayResult;
        setReplayResult({ runId: run.run_id, result: r });
        setSelected(run);
        setDetailView('replay');
        onFlash(r.exact_eligible ? 'success' : 'error', `Exact eligibility: ${r.exact_eligible ? '✓ eligible' : '✗ not eligible'}`);
      } else if (action === 'redact') {
        const res = await api.redact(run.run_id);
        setSecretsState(null);
        setSecretsReloadTick(t => t + 1);
        onFlash('success', `Redaction proof rewritten — ${res.findings_count} finding${res.findings_count === 1 ? '' : 's'}`);
      } else if (action === 'delete') {
        await api.deleteRun(run.run_id);
        setRuns(prev => prev ? prev.filter(r => r.run_id !== run.run_id) : prev);
        if (selected?.run_id === run.run_id) {
          setSelected(null);
          setCapsule(null);
        }
        onFlash('success', `Capsule ${run.run_id} deleted`);
      }
      setActionTarget(null);
    } catch (e) {
      onFlash('error', `${action} failed: ${(e as Error).message}`);
    } finally {
      setActionBusy(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actionTarget, onFlash]);

  const handleValidate = useCallback(async (runId: string) => {
    setValidationStates(prev => ({
      ...prev,
      [runId]: { runId, loading: true, result: null, error: null },
    }));
    try {
      const res = await api.validateRun(runId);
      setValidationStates(prev => ({
        ...prev,
        [runId]: { runId, loading: false, result: { valid: res.valid, errors: res.errors }, error: null },
      }));
    } catch (e) {
      setValidationStates(prev => ({
        ...prev,
        [runId]: { runId, loading: false, result: null, error: (e as Error).message },
      }));
    }
  }, []);

  // Must be declared before early returns to satisfy React's Rules of Hooks
  const visibleRuns = useMemo(() => {
    if (!runs) return [];
    return runs.slice().sort((a, b) => {
      if (sort === 'newest') return (b.created_at ?? '').localeCompare(a.created_at ?? '');
      if (sort === 'oldest') return (a.created_at ?? '').localeCompare(b.created_at ?? '');
      if (sort === 'longest') return (b.duration_ms ?? 0) - (a.duration_ms ?? 0);
      return (a.duration_ms ?? 0) - (b.duration_ms ?? 0);
    });
  }, [runs, sort]);

  if (error && runs === null) return <ErrorBox message={error} onRetry={refresh} />;
  if (runs === null) return <Loading />;

  const actionMeta = buildActionMeta(actionTarget);

  return (
    <div className="grid lg:grid-cols-[320px_1fr] gap-4 h-full">
      {/* Run list panel */}
      <aside className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden flex flex-col" style={{ maxHeight: 'calc(100vh - 3rem)' }}>
        <RunFilters
          visibleCount={visibleRuns.length}
          totalApprox={totalApprox}
          liveConnected={liveConnected}
          onNavigate={onNavigate}
          refresh={refresh}
          search={search}
          setSearch={setSearch}
          statusFilter={statusFilter}
          setStatusFilter={setStatusFilter}
          sort={sort}
          setSort={setSort}
          since={since}
          setSince={setSince}
          until={until}
          setUntil={setUntil}
        />
        <RunList
          visibleRuns={visibleRuns}
          loadedCount={runs.length}
          totalApprox={totalApprox}
          selected={selected}
          onSelect={setSelected}
          checkedIds={checkedIds}
          setCheckedIds={setCheckedIds}
          compareA={compareA}
          setCompareA={setCompareA}
          onCompareTo={onCompareTo}
          costMap={costMap}
          validationStates={validationStates}
          onValidate={handleValidate}
          onAction={(run, action) => setActionTarget({ run, action })}
          onShowSecrets={(r) => { setSelected(r); setDetailView('secrets'); }}
          hasMore={hasMore}
          loadingMore={loadingMore}
          loadMore={loadMore}
        />
      </aside>

      {/* Detail panel */}
      <RunInspector
        selected={selected}
        capsule={capsule}
        detailError={detailError}
        runs={runs}
        isDistributed={isDistributed}
        detailView={detailView}
        setDetailView={setDetailView}
        replayResult={replayResult}
        secretsState={secretsState}
        childrenState={childrenState}
        forensicsState={forensicsState}
        onSelect={setSelected}
        onAction={(run, action) => setActionTarget({ run, action })}
        onCompareTo={onCompareTo}
      />

      {/* v0.46.0 parity panels — span both grid columns, below the runs table area */}
      <div className="lg:col-span-2 space-y-4">
        <CapsuleTreePanel runIds={visibleRuns.map(r => r.run_id)} />
        <RunSpoolLineagePanel runIds={visibleRuns.map(r => r.run_id)} />
        <ScanSecretsPanel runIds={visibleRuns.map(r => r.run_id)} />
      </div>

      <ConfirmDialog
        open={!!actionTarget && !!actionMeta}
        title={actionMeta?.title ?? ''}
        description={actionMeta?.description ?? ''}
        cliEquivalent={actionMeta?.cliEquivalent ?? ''}
        details={
          actionTarget?.action === 'export'
            ? <p className="text-[var(--color-text-muted)]">If <code className="font-mono">~/.novafabric/keys/local-key.pem</code> doesn&apos;t exist, the server will auto-generate a fresh ed25519 keypair (audit-logged). For production use, replace it with your own deployment key.</p>
            : null
        }
        confirmLabel={actionMeta?.confirmLabel ?? 'Confirm'}
        busy={actionBusy}
        onConfirm={onActionConfirm}
        onCancel={() => setActionTarget(null)}
      />
    </div>
  );
}
