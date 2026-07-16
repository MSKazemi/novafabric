import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { clsx } from 'clsx';
import { api, getConnection, openManagedRunStream, type RunStreamHandle } from '../../../lib/api';
import type { RunSummary, FullCapsule, RedactionProof, CapsuleTreeNode } from '../../../lib/api';
import CapsuleInspector from '../../capsule/CapsuleInspector';
import TraceView from '../../capsule/TraceView';
import ConfirmDialog from '../ConfirmDialog';
import { StatusDot, ErrorBox, Loading } from '../helpers';
import EmptyState from '../../ui/EmptyState';
import { SuggestInput } from '../../ui/SuggestInput';
import type { Tab } from '../Sidebar';
import { writeResumeItem } from './HomeTab';

type DetailView = 'inspect' | 'trace' | 'replay' | 'secrets' | 'children';

interface RunCostEntry {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  calls: number;
}

function extractScenario(command: string[]): string | null {
  const idx = command.indexOf('--scenario');
  if (idx < 0 || idx + 1 >= command.length) return null;
  const parts = command[idx + 1].split('/').filter(Boolean);
  // prefer the directory segment before the filename (e.g. "c1_model_contract_break" from
  // "scenarios/c1_model_contract_break/scenario.yaml"), fall back to the filename itself.
  const candidate = parts.length >= 2 ? parts[parts.length - 2] : (parts.pop() ?? '');
  return candidate.replace(/\.yaml$/, '') || null;
}

interface ReplayResult {
  replay_id: string;
  replay_of_run_id: string;
  mode: string;
  status: string;
  start_time: string;
  end_time: string;
  duration_ms: number;
  policy_flags_used: string[];
  env_warnings: Array<Record<string, string>>;
  model_calls_mocked: number;
  tool_calls_mocked: number;
  exit_code?: number | null;
  error?: Record<string, unknown> | null;
  dry_run_report?: string;
  // semantic-mode fields
  similarity_score?: number | null;
  matched_run_id?: string | null;
  // exact-mode fields
  exact_eligible?: boolean | null;
  exact_hash_count?: number | null;
  exact_reasons?: string[] | null;
}

type RunAction = 'export' | 'replay' | 'dry-run' | 'redact' | 'semantic' | 'exact' | 'delete';

interface ValidationState {
  runId: string;
  loading: boolean;
  result: { valid: boolean; errors: string[] } | null;
  error: string | null;
}
type StatusFilter = 'all' | 'running' | 'success' | 'failure' | 'error';
type RunSort = 'newest' | 'oldest' | 'longest' | 'shortest';

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
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
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
  const [secretsState, setSecretsState] = useState<{
    runId: string;
    proof: RedactionProof | null;
    loading: boolean;
    error: string | null;
  } | null>(null);
  const [secretsReloadTick, setSecretsReloadTick] = useState(0);
  const [childrenState, setChildrenState] = useState<{
    runId: string;
    loading: boolean;
    data: { child_count: number; children: Array<{ run_id: string; status: string | null; edge_type: string | null; exit_code: number | null }> } | null;
    error: string | null;
  } | null>(null);
  const [validationStates, setValidationStates] = useState<Record<string, ValidationState>>({});
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [costMap, setCostMap] = useState<Record<string, RunCostEntry>>({});

  // Cursor-based pagination state (B-1)
  const PAGE_SIZE = 50;
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [since, setSince] = useState('');
  const [until, setUntil] = useState('');
  const [totalApprox, setTotalApprox] = useState(0);
  const totalApproxRef = useRef(0);
  const onCountChangeRef = useRef(onCountChange);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // SSE live indicator (B-3)
  const [liveConnected, setLiveConnected] = useState(false);
  const sseRef = useRef<RunStreamHandle | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    setNextCursor(null);
    try {
      const r = await api.searchRuns({
        limit: PAGE_SIZE,
        q: search.trim() || undefined,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        since: since || undefined,
        until: until || undefined,
      });
      setRuns(r.items);
      setNextCursor(r.next_cursor);
      setTotalApprox(r.total_approx);
      totalApproxRef.current = r.total_approx;
      setHasMore(r.next_cursor !== null);
      onCountChange?.(r.total_approx);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [onCountChange, search, since, until, statusFilter]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const r = await api.searchRuns({
        limit: PAGE_SIZE,
        cursor: nextCursor,
        q: search.trim() || undefined,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        since: since || undefined,
        until: until || undefined,
      });
      setRuns(prev => [...(prev ?? []), ...r.items]);
      setNextCursor(r.next_cursor);
      setHasMore(r.next_cursor !== null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  }, [nextCursor, loadingMore, search, since, until, statusFilter]);

  useEffect(() => { refresh(); }, [refresh, refreshTick]);
  // Keep ref current so the SSE callback (captured once) always calls the latest prop
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  // SSE live stream — prepend newly-captured runs (B-3).
  // Managed stream auto-reconnects with backoff and reports connection state.
  useEffect(() => {
    const handle = openManagedRunStream(
      (newRun) => {
        setRuns(prev => {
          if (!prev) return [newRun];
          if (prev.some(r => r.run_id === newRun.run_id)) return prev;
          return [newRun, ...prev];
        });
        const next = totalApproxRef.current + 1;
        totalApproxRef.current = next;
        setTotalApprox(next);
        onCountChangeRef.current?.(next);
      },
      (connected) => setLiveConnected(connected),
    );
    sseRef.current = handle;
    return () => { handle.close(); setLiveConnected(false); };
  // only open once; don't re-open on filter changes
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch per-run cost summary whenever the run list changes.
  // Degrades silently when ClickHouse is absent (endpoint returns {costs:{}}).
  useEffect(() => {
    if (!runs || runs.length === 0) return;
    const { token, base } = getConnection();
    if (!token) return;
    const ids = runs.map(r => r.run_id).join(',');
    const url = `${base}/api/runs/cost-summary?token=${encodeURIComponent(token)}&run_ids=${encodeURIComponent(ids)}`;
    let cancelled = false;
    fetch(url)
      .then(res => res.ok ? res.json() : Promise.resolve({ costs: {} }))
      .then((data: { costs?: Record<string, RunCostEntry> }) => {
        if (!cancelled && data.costs && Object.keys(data.costs).length > 0) {
          setCostMap(prev => ({ ...prev, ...data.costs }));
        }
      })
      .catch(() => { /* ClickHouse absent — degrade silently */ });
    return () => { cancelled = true; };
  }, [runs]);

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

  // Virtual scroll — BL-5: collapse DOM at 10K rows
  const rowVirtualizer = useVirtualizer({
    count: visibleRuns.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 40,
    overscan: 10,
  });

  if (error && runs === null) return <ErrorBox message={error} onRetry={refresh} />;
  if (runs === null) return <Loading />;

  const actionMeta = actionTarget && {
    title: actionTarget.action === 'export' ? 'Export signed evidence bundle'
         : actionTarget.action === 'replay' ? 'Run forensic replay'
         : actionTarget.action === 'dry-run' ? 'Dry-run policy check'
         : actionTarget.action === 'semantic' ? 'Semantic similarity analysis'
         : actionTarget.action === 'exact' ? 'Exact replay eligibility check'
         : actionTarget.action === 'delete' ? 'Delete capsule'
         : 'Re-scan & redact capsule',
    description: actionTarget.action === 'export'
      ? `Build a tamper-evident, ed25519-signed ZIP from this capsule. Output goes to ~/.novafabric/evidence/${actionTarget.run.run_id}.zip.`
      : actionTarget.action === 'replay'
      ? 'Forensic mode: read-only inspection of what the capsule recorded. No subprocess spawned, no network calls. Safe to run anywhere.'
      : actionTarget.action === 'dry-run'
      ? 'Check which tool calls in this capsule would be blocked by the current replay policy — without executing anything. Reports ALLOW / BLOCK per tool call.'
      : actionTarget.action === 'semantic'
      ? 'Compute pairwise text similarity across model call responses in this capsule. Reports a similarity score (0–100%). Read-only.'
      : actionTarget.action === 'exact'
      ? 'Check whether this capsule meets exact replay requirements: deterministic env.lock and seeded model calls. Read-only.'
      : actionTarget.action === 'delete'
      ? `This permanently deletes capsule ${actionTarget.run.run_id} and cannot be undone. Blocked if any active legal hold exists.`
      : 'Re-run the secret scanner against this capsule and overwrite redaction-proof.json. Existing unsafe_skips are preserved.',
    cliEquivalent: actionTarget.action === 'export'
      ? `nova export-evidence ${actionTarget.run.capsule_path} --output ~/.novafabric/evidence/${actionTarget.run.run_id}.zip`
      : actionTarget.action === 'replay'
      ? `nova replay ${actionTarget.run.capsule_path} --mode forensic`
      : actionTarget.action === 'dry-run'
      ? `nova replay ${actionTarget.run.capsule_path} --mode forensic --dry-run`
      : actionTarget.action === 'semantic'
      ? `nova replay ${actionTarget.run.capsule_path} --mode semantic`
      : actionTarget.action === 'exact'
      ? `nova replay ${actionTarget.run.capsule_path} --mode exact`
      : actionTarget.action === 'delete'
      ? `nova capsule delete ${actionTarget.run.run_id} --registry default`
      : `nova redact ${actionTarget.run.capsule_path}`,
    confirmLabel: actionTarget.action === 'export' ? 'Export bundle'
                : actionTarget.action === 'replay' ? 'Run forensic replay'
                : actionTarget.action === 'dry-run' ? 'Run dry-run check'
                : actionTarget.action === 'semantic' ? 'Run semantic analysis'
                : actionTarget.action === 'exact' ? 'Run exact eligibility check'
                : actionTarget.action === 'delete' ? 'Delete capsule'
                : 'Re-scan & write proof',
  };

  return (
    <div className="grid lg:grid-cols-[320px_1fr] gap-4 h-full">
      {/* Run list panel */}
      <aside className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden flex flex-col" style={{ maxHeight: 'calc(100vh - 3rem)' }}>
        <header className="px-3 py-2 border-b border-[var(--color-border)] space-y-2 shrink-0">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-medium text-[var(--color-text)]">
              Runs
              <span className="text-[var(--color-text-faint)] font-mono ml-1.5">
                ({visibleRuns.length}{totalApprox > visibleRuns.length ? ` of ~${totalApprox}` : ''})
              </span>
            </h3>
            <div className="flex items-center gap-2">
              {liveConnected && (
                <span
                  title="Live stream connected — new runs appear automatically"
                  className="flex items-center gap-1 text-[9px] font-mono text-[var(--color-status-success)]"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-status-success)] animate-pulse inline-block" />
                  live
                </span>
              )}
              {onNavigate && (
                <button
                  onClick={() => onNavigate('commands')}
                  className="text-[10px] text-[var(--color-accent)] hover:underline"
                >
                  ▶ Capture new run
                </button>
              )}
              <button onClick={refresh} className="text-[10px] text-[var(--color-text-muted)] hover:text-[var(--color-text)]">↻ refresh</button>
            </div>
          </div>
          <input
            type="search"
            value={search}
            onChange={e => setSearch(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && refresh()}
            placeholder="Search run_id or command… (Enter to search)"
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
          <div className="flex gap-2 flex-wrap">
            {(['all', 'running', 'success', 'failure', 'error'] as StatusFilter[]).map(s => (
              <button
                key={s}
                type="button"
                onClick={() => { setStatusFilter(s); }}
                className={clsx(
                  'px-2 py-0.5 rounded border text-[10px] uppercase tracking-wider font-medium transition-colors',
                  statusFilter === s
                    ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)] border-[var(--color-accent)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                )}
              >
                {s}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2 text-[10px]">
            <select value={sort} onChange={e => setSort(e.target.value as RunSort)}
              className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-1 font-mono">
              <option value="newest">newest first</option>
              <option value="oldest">oldest first</option>
              <option value="longest">longest first</option>
              <option value="shortest">shortest first</option>
            </select>
          </div>
          {/* Date filter */}
          <div className="flex gap-1.5 items-center flex-wrap text-[10px]">
            <label className="text-[var(--color-text-faint)]">From</label>
            <input
              type="date"
              value={since}
              onChange={e => setSince(e.target.value)}
              className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1 py-0.5 font-mono"
            />
            <label className="text-[var(--color-text-faint)]">To</label>
            <input
              type="date"
              value={until}
              onChange={e => setUntil(e.target.value)}
              className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1 py-0.5 font-mono"
            />
            {(since || until) && (
              <button
                onClick={() => { setSince(''); setUntil(''); }}
                className="px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] cursor-pointer"
              >
                Clear
              </button>
            )}
          </div>
        </header>

        {/* Compare selected banner — shown when 2–5 checkboxes are checked */}
        {checkedIds.length >= 2 && (
          <div className="px-3 py-2 flex items-center gap-2 border-b border-[var(--color-border)] bg-[color-mix(in_oklab,var(--color-accent)_8%,var(--color-bg-raised))] shrink-0">
            <span className="flex-1 text-[10px] font-mono text-[var(--color-text-muted)] truncate">
              {checkedIds.length} runs selected
            </span>
            <button
              type="button"
              onClick={() => {
                if (checkedIds.length >= 2 && onCompareTo) {
                  onCompareTo(checkedIds);
                  setCheckedIds([]);
                }
              }}
              className="shrink-0 px-2.5 py-1 text-[10px] rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] transition-colors"
            >
              Compare selected ⊕
            </button>
            <button
              type="button"
              onClick={() => setCheckedIds([])}
              title="Clear selection"
              className="shrink-0 text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-xs leading-none"
            >
              ×
            </button>
          </div>
        )}

        {compareA && (
          <div className="px-3 py-2 flex items-center gap-2 border-b border-[var(--color-border)] bg-[color-mix(in_oklab,var(--color-accent)_8%,var(--color-bg-raised))] text-[10px] font-mono text-[var(--color-text-muted)] shrink-0">
            <span className="flex-1 truncate">
              Run <code className="text-[var(--color-accent)]">{compareA.slice(0, 10)}…</code> selected as A — click another run&apos;s Cmp to compare
            </span>
            <button
              type="button"
              onClick={() => setCompareA(null)}
              title="Cancel selection"
              className="shrink-0 text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-xs leading-none"
            >
              ×
            </button>
          </div>
        )}

        {visibleRuns.length === 0 ? (
          runs.length === 0
            ? <EmptyState
                message="No capsules yet."
                cliCommand="nova capture <cmd>"
                variant="inline"
              />
            : <EmptyState message="No runs match the current filter." variant="inline" />
        ) : (
          <div
            ref={listRef}
            className="overflow-y-auto flex-1"
            style={{ maxHeight: 600 }}
          >
            <div
              style={{ height: rowVirtualizer.getTotalSize(), position: 'relative' }}
            >
              {rowVirtualizer.getVirtualItems().map(virtualRow => {
                const r = visibleRuns[virtualRow.index];
                if (!r) return null;
                const isChecked = checkedIds.includes(r.run_id);
                const atLimit = checkedIds.length >= 5 && !isChecked;
                return (
              <div
                key={r.run_id}
                data-index={virtualRow.index}
                ref={rowVirtualizer.measureElement}
                style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${virtualRow.start}px)` }}
                className={clsx(
                  'border-b border-[var(--color-border)]',
                  selected?.run_id === r.run_id && 'bg-[color-mix(in_oklab,var(--color-accent)_6%,var(--color-bg-sunken))]',
                )}
              >
                <div className="flex items-start">
                  {/* Checkbox column (36px wide) */}
                  <div className="flex items-center justify-center w-9 shrink-0 pt-3">
                    <input
                      type="checkbox"
                      checked={isChecked}
                      disabled={atLimit}
                      title={atLimit ? 'Maximum 5 runs selected — deselect one first' : isChecked ? 'Deselect' : 'Select for comparison (up to 5)'}
                      onChange={() => {
                        if (isChecked) {
                          setCheckedIds(prev => prev.filter(id => id !== r.run_id));
                        } else if (checkedIds.length < 5) {
                          setCheckedIds(prev => [...prev, r.run_id]);
                        }
                      }}
                      className="w-3.5 h-3.5 rounded border border-[var(--color-border)] accent-[var(--color-accent)] cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed"
                    />
                  </div>
                  {/* Run content — stacks select button + action buttons vertically */}
                  <div className="flex-1 flex flex-col min-w-0">
                <button
                  onClick={() => {
                    setSelected(r);
                    writeResumeItem({
                      tab: 'runs',
                      label: `Inspecting run ${r.run_id.slice(0, 8)}`,
                      meta: `${r.command.join(' ').slice(0, 40)} · ${r.status ?? '—'}`,
                      icon: '🔍',
                    });
                  }}
                  className="text-left px-3 py-2.5 hover:bg-[var(--color-bg-sunken)] transition-colors"
                >
                  {/* Row: status + id + duration */}
                  <div className="flex items-center gap-2 mb-0.5">
                    <StatusDot status={r.status} />
                    <div className="relative group flex-1 min-w-0 flex items-center">
                      <code className="font-mono text-xs text-[var(--color-text)] truncate">{r.run_id.slice(0, 16)}…</code>
                      <button
                        type="button"
                        onClick={e => {
                          e.stopPropagation();
                          navigator.clipboard.writeText(r.run_id);
                          setCopiedId(r.run_id);
                          setTimeout(() => setCopiedId(null), 1500);
                        }}
                        title="Copy run ID"
                        className="opacity-0 group-hover:opacity-100 transition-opacity absolute right-0 top-1/2 -translate-y-1/2 text-[var(--color-text-faint)] hover:text-[var(--color-text)] px-0.5"
                      >
                        {copiedId === r.run_id ? (
                          <span className="text-[9px] font-mono text-[var(--color-status-success)]">Copied</span>
                        ) : (
                          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3">
                            <path d="M5 3a1 1 0 000 2h6a1 1 0 100-2H5z"/>
                            <path fillRule="evenodd" d="M3 5a2 2 0 012-2h6a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V5zm2 1a1 1 0 000 2h6a1 1 0 100-2H5zm0 3a1 1 0 000 2h4a1 1 0 100-2H5z" clipRule="evenodd"/>
                          </svg>
                        )}
                      </button>
                    </div>
                    <span className="text-[10px] text-[var(--color-text-faint)] font-mono shrink-0 tabular-nums">
                      {r.duration_ms != null ? `${r.duration_ms}ms` : '—'}
                    </span>
                  </div>
                  {/* Command */}
                  <p className="text-[10px] text-[var(--color-text-muted)] font-mono truncate" title={r.command.join(' ')}>
                    {r.command.join(' ')}
                  </p>
                  {extractScenario(r.command) && (
                    <span className="text-[9px] font-mono px-1.5 py-px rounded bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)] truncate max-w-[140px]">
                      {extractScenario(r.command)}
                    </span>
                  )}
                  {/* Meta row */}
                  <div className="flex items-center gap-3 mt-0.5 text-[9px] text-[var(--color-text-faint)] font-mono">
                    <span>{r.created_at?.slice(0, 16) ?? '—'}</span>
                    <span>{r.model_call_count}m · {r.tool_call_count}t</span>
                    {r.exit_code !== null && r.exit_code !== 0 && (
                      <span className="text-[var(--color-status-failure)]">exit {r.exit_code}</span>
                    )}
                  </div>
                  {/* Cost row — only rendered when ClickHouse data is available */}
                  {costMap[r.run_id] && (
                    <div className="flex items-center gap-3 mt-0.5 text-[9px] text-[var(--color-text-faint)] font-mono">
                      <span title="Estimated LLM cost (ClickHouse)">
                        {costMap[r.run_id].cost_usd > 0
                          ? `$${costMap[r.run_id].cost_usd.toFixed(6)}`
                          : '—'}
                      </span>
                      <span title="Model API calls tracked in ClickHouse">
                        {costMap[r.run_id].calls > 0 ? `${costMap[r.run_id].calls} calls` : '—'}
                      </span>
                    </div>
                  )}
                </button>
                {/* Action buttons */}
                <div className="px-3 pb-2 space-y-1">
                  {/* Replay row: all inspection modes + compare + export */}
                  <div className="flex items-center gap-1 flex-wrap">
                    <button
                      onClick={() => setActionTarget({ run: r, action: 'replay' })}
                      title="Forensic replay (read-only inspection)"
                      className="px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                    >replay</button>
                    <button
                      onClick={() => setActionTarget({ run: r, action: 'dry-run' })}
                      title="Dry-run policy check — which tools would be blocked?"
                      className="px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-status-pending)] hover:border-[var(--color-status-pending)] uppercase tracking-wider font-medium"
                    >dry-run</button>
                    <button
                      onClick={() => setActionTarget({ run: r, action: 'semantic' })}
                      title="Semantic analysis — compute similarity score across model call responses"
                      className="px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-status-pending)] hover:border-[var(--color-status-pending)] uppercase tracking-wider font-medium"
                    >semantic</button>
                    <button
                      onClick={() => setActionTarget({ run: r, action: 'exact' })}
                      title="Exact eligibility — check whether this capsule can be replayed byte-exactly"
                      className="px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                    >exact</button>
                    {(() => {
                      const isCompareA = compareA === r.run_id;
                      const canCompareVs = !!compareA && compareA !== r.run_id;
                      const buttonLabel = isCompareA ? 'A ×' : canCompareVs ? 'vs A' : 'Cmp';
                      const buttonTitle = isCompareA
                        ? 'Cancel selection'
                        : canCompareVs
                        ? `Compare against ${compareA!.slice(0, 8)}…`
                        : 'Select as baseline for comparison';
                      return (
                        <button
                          type="button"
                          title={buttonTitle}
                          onClick={() => {
                            if (isCompareA) { setCompareA(null); return; }
                            if (canCompareVs) { onCompareTo?.([compareA!, r.run_id]); setCompareA(null); return; }
                            setCompareA(r.run_id);
                          }}
                          disabled={!onCompareTo || (runs?.length ?? 0) < 2}
                          className={clsx(
                            'px-1.5 py-0.5 rounded text-[10px] border transition-colors shrink-0',
                            isCompareA
                              ? 'border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                              : canCompareVs
                              ? 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]'
                              : 'border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]',
                            (!onCompareTo || (runs?.length ?? 0) < 2) && 'opacity-40 cursor-not-allowed',
                          )}
                        >
                          {buttonLabel}
                        </button>
                      );
                    })()}
                    <button
                      onClick={() => setActionTarget({ run: r, action: 'export' })}
                      title="Export signed evidence bundle"
                      className="ml-auto px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-accent)] hover:border-[var(--color-accent)] uppercase tracking-wider font-medium"
                    >export ↗</button>
                  </div>
                  {/* Data ops row: validate + redact + secrets */}
                  {(() => {
                    const vs = validationStates[r.run_id];
                    return (
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <button
                          onClick={() => handleValidate(r.run_id)}
                          disabled={vs?.loading === true}
                          title="Validate capsule schema and required files"
                          className="px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {vs?.loading ? 'validating…' : 'validate'}
                        </button>
                        {vs && !vs.loading && vs.result !== null && (
                          vs.result.valid ? (
                            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-mono font-medium border border-[color-mix(in_oklab,var(--color-status-success)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]">
                              ✓ valid
                            </span>
                          ) : (
                            <ValidationErrorBadge errors={vs.result.errors} />
                          )
                        )}
                        {vs && !vs.loading && vs.error !== null && (
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-mono font-medium border border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]" title={vs.error}>
                            ✗ error
                          </span>
                        )}
                        <button
                          onClick={() => setActionTarget({ run: r, action: 'redact' })}
                          title="Re-scan & rewrite redaction-proof.json"
                          className="px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                        >redact</button>
                        <button
                          onClick={() => { setSelected(r); setDetailView('secrets'); }}
                          title="View secret scan / redaction proof"
                          className="px-1.5 py-0.5 text-[9px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                        >secrets</button>
                        <button
                          onClick={() => setActionTarget({ run: r, action: 'delete' })}
                          title="Permanently delete this capsule"
                          className="px-1.5 py-0.5 text-[9px] rounded border border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] uppercase tracking-wider font-medium ml-auto"
                        >delete</button>
                      </div>
                    );
                  })()}
                </div>
                  </div>{/* end content column */}
                </div>{/* end flex items-start */}
              </div>
              );
              })}
            </div>
          </div>
        )}

        {/* Load more (cursor pagination — B-1) */}
        {hasMore && (
          <div className="px-3 py-2 border-t border-[var(--color-border)] shrink-0">
            <button
              onClick={loadMore}
              disabled={loadingMore}
              className="w-full px-2 py-1.5 text-[10px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {loadingMore ? 'Loading…' : `Load more (~${totalApprox - visibleRuns.length} remaining)`}
            </button>
          </div>
        )}
      </aside>

      {/* Detail panel */}
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
                      onClick={() => { if (parentRun) setSelected(parentRun); }}
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
                    <span className="text-[9px] font-mono uppercase text-[var(--color-text-faint)] px-1.5 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">
                      worker
                    </span>
                  </div>
                );
              }

              if (capsuleType === 'parent' && workerRunIds && workerRunIds.length > 0) {
                return (
                  <ValidateDistributedBlock runId={manifest.run_id as string} workerCount={workerRunIds.length} runs={runs ?? []} workerRunIds={workerRunIds} onJump={(r) => { const found = runs?.find(x => x.run_id === r); if (found) setSelected(found); }} />
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
                  onRunRedact={() => setActionTarget({ run: sel, action: 'redact' })}
                />
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
                          <span className="text-[9px] px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
                            {child.edge_type}
                          </span>
                        )}
                        {child.status && (
                          <span className={clsx(
                            'text-[9px] px-1.5 py-0.5 rounded',
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
          <span className="text-[9px] font-mono uppercase text-[var(--color-accent)] px-1.5 py-px rounded bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] border border-[color-mix(in_oklab,var(--color-accent)_30%,transparent)]">
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
                'font-mono text-[9px] px-2 py-px rounded border transition-colors',
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
            <span className="ml-2 text-[9px] opacity-70">[{result.status}]</span>
          )}
        </div>
      )}

      <p className="text-[9px] font-mono text-[var(--color-text-faint)]">
        CLI: <code>nova validate-distributed {runId}</code>
      </p>
    </div>
  );
}

function ValidationErrorBadge({ errors }: { errors: string[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <span className="inline-flex flex-col gap-0.5">
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-mono font-medium border border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]"
      >
        ✗ {errors.length} error{errors.length !== 1 ? 's' : ''} {expanded ? '▲' : '▼'}
      </button>
      {expanded && (
        <ul className="mt-0.5 ml-0.5 space-y-0.5 max-w-[220px]">
          {errors.map((err, i) => (
            <li key={i} className="text-[8px] font-mono text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_20%,transparent)] rounded px-1.5 py-0.5 break-all">
              {err}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

const SEVERITY_STYLE: Record<string, string> = {
  critical: 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)]',
  high: 'text-[color-mix(in_oklab,var(--color-status-failure)_70%,var(--color-status-pending))] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)] border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]',
  medium: 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)] border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]',
  low: 'text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] border-[var(--color-border)]',
  info: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)] border-[var(--color-border)]',
};

function SeverityBadge({ severity }: { severity: string }) {
  const cls = SEVERITY_STYLE[severity] ?? SEVERITY_STYLE.info;
  return (
    <span className={`inline-block font-mono text-[9px] uppercase px-1.5 py-px rounded border ${cls}`}>
      {severity}
    </span>
  );
}

function SecretScanPanel({
  runId,
  capsulePath,
  proof,
  loading,
  error,
  onRunRedact,
}: {
  runId: string;
  capsulePath: string;
  proof: RedactionProof | null;
  loading: boolean;
  error: string | null;
  onRunRedact: () => void;
}) {
  if (loading) return <Loading />;

  if (error) {
    const notFound = error.includes('404') || error.toLowerCase().includes('not found');
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-8 text-center space-y-3">
        <p className="text-sm text-[var(--color-text-muted)]">
          {notFound
            ? 'No redaction proof found for this run.'
            : `Failed to load: ${error}`}
        </p>
        {notFound && (
          <>
            <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
              CLI equivalent: <code>nova redact {capsulePath}</code>
            </p>
            <button
              onClick={onRunRedact}
              className="px-3 py-1.5 text-xs rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] font-medium"
            >
              Run redact now
            </button>
          </>
        )}
      </div>
    );
  }

  if (!proof) return null;

  const totalFindings = proof.findings_count.total;
  const isClean = totalFindings === 0;
  const bySev = proof.findings_count.by_severity;

  return (
    <div className="space-y-4">
      {/* Summary header */}
      <div className={clsx(
        'rounded-lg border p-4',
        isClean
          ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)]'
          : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)]',
      )}>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <span className={clsx(
              'text-base font-mono font-semibold',
              isClean ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
            )}>
              {isClean ? '✓ Clean' : `⚠ ${totalFindings} finding${totalFindings !== 1 ? 's' : ''}`}
            </span>
            {!isClean && (
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {(['critical', 'high', 'medium', 'low', 'info'] as const).map(s =>
                  (bySev[s] ?? 0) > 0
                    ? (
                      <span
                        key={s}
                        className={`inline-block font-mono text-[9px] uppercase px-1.5 py-px rounded border ${SEVERITY_STYLE[s] ?? SEVERITY_STYLE.info}`}
                      >
                        {s} ×{bySev[s]}
                      </span>
                    )
                    : null
                )}
              </div>
            )}
          </div>
          <div className="text-right text-[10px] font-mono text-[var(--color-text-faint)] space-y-0.5">
            <div>{proof.bytes_scanned.toLocaleString()} B scanned</div>
            <div>{proof.bytes_redacted.toLocaleString()} B redacted</div>
          </div>
        </div>
        <div className="mt-2 text-[9px] font-mono text-[var(--color-text-faint)] space-y-0.5">
          <div>proof_id: {proof.proof_id}  ·  {proof.created_at.slice(0, 16)}</div>
          <div className="truncate">chain: {proof.chain_hash}</div>
        </div>
      </div>

      {/* Scanner + packs */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Scanner</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">name</dt>
          <dd className="text-[var(--color-text-muted)]">{proof.scanner.name} v{proof.scanner.version}</dd>
          <dt className="text-[var(--color-text-faint)]">engine</dt>
          <dd className="text-[var(--color-text-muted)]">{proof.scanner.engine} v{proof.scanner.engine_version}</dd>
        </dl>
        {proof.packs.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {proof.packs.map(p => (
              <span key={p.name} className="text-[9px] font-mono px-1.5 py-px rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)]">
                {p.name} v{p.version} ({p.rules_count} rules)
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Targets */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">
          Targets ({proof.targets.length})
        </h4>
        <div className="overflow-x-auto">
          <table className="w-full text-[10px] font-mono text-[var(--color-text-muted)]">
            <thead>
              <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                <th className="text-left pb-1.5 pr-3">kind</th>
                <th className="text-left pb-1.5 pr-3">ref</th>
                <th className="text-right pb-1.5 pr-3">bytes</th>
                <th className="text-right pb-1.5 pr-3">findings</th>
                <th className="text-center pb-1.5">hash status</th>
              </tr>
            </thead>
            <tbody>
              {proof.targets.map(t => {
                const dirty = t.hash_before_redaction !== t.hash_after_redaction;
                return (
                  <tr key={t.ref} className="border-b border-[var(--color-border)] last:border-0">
                    <td className="py-1.5 pr-3 text-[var(--color-text-faint)]">{t.kind}</td>
                    <td className="py-1.5 pr-3">{t.ref}</td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">{t.bytes_scanned.toLocaleString()}</td>
                    <td className={clsx('py-1.5 pr-3 text-right tabular-nums', t.findings_count > 0 ? 'text-[var(--color-status-failure)]' : '')}>
                      {t.findings_count}
                    </td>
                    <td className="py-1.5 text-center">
                      {t.skipped
                        ? <span className="text-[var(--color-text-faint)]">skipped</span>
                        : dirty
                        ? <span className="text-[var(--color-status-pending)]">redacted</span>
                        : <span className="text-[var(--color-status-success)]">✓ clean</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Findings detail */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">
          Findings ({totalFindings})
        </h4>
        {totalFindings === 0 ? (
          <p className="text-xs text-[var(--color-text-muted)]">No secrets detected in this capsule.</p>
        ) : (
          <ul className="space-y-3">
            {proof.findings.map(f => (
              <li key={f.finding_id} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 space-y-1.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <SeverityBadge severity={f.severity} />
                  <code className="text-xs text-[var(--color-text)]">{f.rule_id}</code>
                  <span className="text-[10px] text-[var(--color-text-faint)] font-mono ml-auto">{f.pack}</span>
                </div>
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[10px] font-mono text-[var(--color-text-muted)]">
                  <dt className="text-[var(--color-text-faint)]">target</dt>
                  <dd>{f.target_ref} @ offset {f.byte_offset} ({f.byte_length}B)</dd>
                  <dt className="text-[var(--color-text-faint)]">match_hash</dt>
                  <dd className="truncate">{f.match_hash}</dd>
                  <dt className="text-[var(--color-text-faint)]">strategy</dt>
                  <dd>{f.redaction_strategy} → <code className="text-[var(--color-text-faint)]">{f.replacement}</code></dd>
                </dl>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Re-run action */}
      <div className="flex justify-end">
        <button
          onClick={onRunRedact}
          className="px-3 py-1.5 text-[10px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] font-mono uppercase tracking-wider"
        >
          Re-scan &amp; rewrite proof
        </button>
      </div>
    </div>
  );
}

function ForensicResultPane({ result }: { result: ReplayResult }) {
  const isDryRun = result.status === 'dry_run';
  const ok = result.status === 'success' || isDryRun;
  const blockedCount = isDryRun && result.dry_run_report
    ? (result.dry_run_report.match(/\bBLOCK\b/g) ?? []).length
    : 0;
  const statusColor = ok && (!isDryRun || blockedCount === 0)
    ? 'text-[var(--color-status-success)]'
    : isDryRun && blockedCount > 0
    ? 'text-[var(--color-status-pending)]'
    : 'text-[var(--color-status-failure)]';

  return (
    <div className="space-y-4">
      {/* Header row */}
      <div className={clsx(
        'rounded-lg border p-4',
        isDryRun && blockedCount > 0
          ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
          : ok
          ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]'
          : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]',
      )}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={clsx('text-lg font-mono font-medium', statusColor)}>
              {ok && !isDryRun ? '✓' : isDryRun ? (blockedCount > 0 ? `⚠ ${blockedCount} blocked` : '✓ all pass') : '✗'}{' '}
              {result.status}
            </span>
            <span className="text-xs text-[var(--color-text-faint)] font-mono uppercase tracking-wider">{result.mode}</span>
          </div>
          <span className="text-xs text-[var(--color-text-faint)] font-mono">{result.duration_ms} ms</span>
        </div>
        <p className="mt-1 text-xs font-mono text-[var(--color-text-muted)] break-all">
          replay_id: {result.replay_id}
        </p>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: isDryRun ? 'model calls (skipped)' : 'model calls inspected', value: result.model_calls_mocked },
          { label: isDryRun ? 'tool calls checked' : 'tool calls inspected', value: result.tool_calls_mocked },
          { label: 'env warnings', value: result.env_warnings.length },
        ].map(({ label, value }) => (
          <div key={label} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 text-center">
            <span className="block font-mono text-xl text-[var(--color-text)]">{value}</span>
            <span className="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">{label}</span>
          </div>
        ))}
      </div>

      {/* Timing */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Timing</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">start</dt>
          <dd className="text-[var(--color-text-muted)]">{result.start_time}</dd>
          <dt className="text-[var(--color-text-faint)]">end</dt>
          <dd className="text-[var(--color-text-muted)]">{result.end_time}</dd>
          {result.exit_code != null && (
            <>
              <dt className="text-[var(--color-text-faint)]">exit_code</dt>
              <dd className={result.exit_code === 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>
                {result.exit_code}
              </dd>
            </>
          )}
        </dl>
      </div>

      {/* Policy flags */}
      {result.policy_flags_used.length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Policy flags</h4>
          <div className="flex flex-wrap gap-1.5">
            {result.policy_flags_used.map(f => (
              <code key={f} className="text-[10px] px-1.5 py-0.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] text-[var(--color-text-muted)]">{f}</code>
            ))}
          </div>
        </div>
      )}

      {/* Env warnings */}
      {result.env_warnings.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-2">
            Environment warnings ({result.env_warnings.length})
          </h4>
          <ul className="space-y-2 text-xs font-mono text-[var(--color-text-muted)]">
            {result.env_warnings.map((w, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-[var(--color-status-pending)] shrink-0">⚠</span>
                <span>{w.field ?? w.key ?? JSON.stringify(w)}: {w.message ?? w.detail ?? ''}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Dry-run policy report */}
      {isDryRun && result.dry_run_report && (
        <div className={clsx(
          'rounded-lg border p-4',
          blockedCount > 0
            ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]'
            : 'border-[var(--color-border)]',
          'bg-[var(--color-bg-raised)]',
        )}>
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">
            Policy report
            {blockedCount > 0 && (
              <span className="ml-2 text-[var(--color-status-pending)]">— {blockedCount} BLOCK{blockedCount !== 1 ? 'S' : ''}</span>
            )}
          </h4>
          <pre className="text-xs font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all leading-relaxed overflow-auto max-h-80">
            {result.dry_run_report}
          </pre>
        </div>
      )}

      {/* Error detail */}
      {result.error && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-failure)] font-mono mb-2">Error</h4>
          <pre className="text-xs font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all">
            {JSON.stringify(result.error, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

function TreeNodeRow({ node, depth }: { node: CapsuleTreeNode; depth: number }) {
  return (
    <>
      <div className="flex items-center gap-2 text-xs font-mono py-0.5 min-w-0" style={{ paddingLeft: depth * 16 }}>
        <span className="shrink-0 text-[9px] uppercase text-[var(--color-accent)] px-1.5 py-px rounded bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] border border-[color-mix(in_oklab,var(--color-accent)_30%,transparent)]">
          {node.capsule_role}
        </span>
        <code className="text-[var(--color-text)] truncate">{node.run_id}</code>
        <span className="shrink-0 text-[10px] text-[var(--color-text-faint)]">{node.status}</span>
        {node.is_synthetic && (
          <span className="shrink-0 text-[9px] text-[var(--color-text-faint)] italic">(synthetic)</span>
        )}
      </div>
      {node.children.map(c => <TreeNodeRow key={c.run_id} node={c} depth={depth + 1} />)}
    </>
  );
}

function CapsuleTreePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<{ root: CapsuleTreeNode; total_nodes: number; orphans: CapsuleTreeNode[] } | null>(null);

  async function load() {
    if (!runId.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.runTree(runId.trim());
      setData(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header>
        <h3 className="text-xs font-medium text-[var(--color-text)]">Distributed Capsule Tree</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
          Parent/child capsule hierarchy — <code>nova run show --with-children</code>
        </p>
      </header>
      <div className="flex gap-2 items-start flex-wrap">
        <div className="flex-1 min-w-[220px] max-w-md">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={load}
            placeholder="run_id"
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={load}
          disabled={busy || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60 transition-colors"
        >
          {busy ? 'Loading…' : 'Load tree'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {data && (
        <div className="space-y-2">
          <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            {data.total_nodes} node{data.total_nodes !== 1 ? 's' : ''}
          </p>
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 overflow-x-auto">
            <TreeNodeRow node={data.root} depth={0} />
          </div>
          {data.orphans.length > 0 && (
            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-3">
              <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-1">
                Orphans ({data.orphans.length})
              </h4>
              {data.orphans.map(o => <TreeNodeRow key={o.run_id} node={o} depth={0} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const SPOOL_EDGE_TYPES = ['contains', 'spawned', 'delegated_to', 'replayed_from'] as const;

function RunSpoolLineagePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [edgeTypes, setEdgeTypes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<{ edges: Array<Record<string, unknown>>; count: number } | null>(null);

  async function query() {
    if (!runId.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.runSpoolLineage(runId.trim(), edgeTypes.length > 0 ? edgeTypes.join(',') : undefined);
      setData(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header>
        <h3 className="text-xs font-medium text-[var(--color-text)]">Run Lineage Edges</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
          Spool lineage edges with edge-type filter — <code>nova run lineage</code>
        </p>
      </header>
      <div className="flex gap-2 items-start flex-wrap">
        <div className="flex-1 min-w-[220px] max-w-md">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={query}
            placeholder="run_id"
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={query}
          disabled={busy || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60 transition-colors"
        >
          {busy ? 'Querying…' : 'Query'}
        </button>
      </div>
      {/* Edge-type filter — none checked = no filter */}
      <div className="flex items-center gap-3 flex-wrap text-[10px] font-mono text-[var(--color-text-muted)]">
        {SPOOL_EDGE_TYPES.map(et => (
          <label key={et} className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="checkbox"
              checked={edgeTypes.includes(et)}
              onChange={() =>
                setEdgeTypes(prev => prev.includes(et) ? prev.filter(x => x !== et) : [...prev, et])
              }
              className="w-3 h-3 rounded border border-[var(--color-border)] accent-[var(--color-accent)] cursor-pointer"
            />
            {et}
          </label>
        ))}
        <span className="text-[var(--color-text-faint)]">(none checked = no filter)</span>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {data && (
        <div className="space-y-2">
          <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            {data.count} edge{data.count !== 1 ? 's' : ''}
          </p>
          {data.edges.length === 0 ? (
            <p className="text-xs text-[var(--color-text-muted)]">No lineage edges recorded for this run.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono text-[var(--color-text-muted)]">
                <thead>
                  <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                    <th className="text-left pb-1.5 pr-3">source_run_id</th>
                    <th className="text-left pb-1.5 pr-3">edge_type</th>
                    <th className="text-left pb-1.5">target_run_id</th>
                  </tr>
                </thead>
                <tbody>
                  {data.edges.map((e, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)] last:border-0">
                      <td className="py-1.5 pr-3 break-all">{String(e.source_run_id ?? '—')}</td>
                      <td className="py-1.5 pr-3">
                        <span className="text-[9px] px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
                          {String(e.edge_type ?? '—')}
                        </span>
                      </td>
                      <td className="py-1.5 break-all">{String(e.target_run_id ?? '—')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface ScanSecretsResult {
  ok: boolean;
  run_id: string;
  findings_count: { total?: number; by_severity?: Record<string, number> };
  findings: Array<{ rule_id: string; severity: string; target_ref: string; redaction_strategy: string }>;
  fail_on: string | null;
  triggered: boolean;
  triggered_count: number;
}

function ScanSecretsPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [failOn, setFailOn] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<ScanSecretsResult | null>(null);

  async function scan() {
    if (!runId.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.scanSecrets(runId.trim(), failOn || undefined);
      setData(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const notFound = err !== null && (err.includes('404') || err.toLowerCase().includes('not found'));
  const total = data?.findings_count.total ?? data?.findings.length ?? 0;
  const bySev = data?.findings_count.by_severity ?? {};

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header>
        <h3 className="text-xs font-medium text-[var(--color-text)]">Secret Scan</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
          Secrets/PII findings from the redaction log — <code>nova scan-secrets</code>
        </p>
      </header>
      <div className="flex gap-2 items-start flex-wrap">
        <div className="flex-1 min-w-[220px] max-w-md">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={scan}
            placeholder="run_id"
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <select
          value={failOn}
          onChange={e => setFailOn(e.target.value)}
          title="Fail threshold — triggers FAIL when findings at or above this severity exist"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
        >
          <option value="">fail-on: none</option>
          {(['critical', 'high', 'medium', 'low', 'info'] as const).map(s => (
            <option key={s} value={s}>fail-on: {s}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={scan}
          disabled={busy || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60 transition-colors"
        >
          {busy ? 'Scanning…' : 'Scan'}
        </button>
      </div>
      {err && (
        <div className="text-xs font-mono space-y-1">
          <p className="text-[var(--color-status-failure)]">{err}</p>
          {notFound && (
            <p className="text-[var(--color-text-faint)]">
              No redaction-proof.json for this capsule — run <code>nova redact &lt;capsule&gt;</code> first.
            </p>
          )}
        </div>
      )}
      {data && (
        <div className="space-y-3">
          {/* PASS/FAIL badge — only when a fail-on threshold was set */}
          {data.fail_on && (
            <div className={clsx(
              'rounded border px-3 py-2 text-xs font-mono font-semibold',
              data.triggered
                ? 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]'
                : 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]',
            )}>
              {data.triggered
                ? `✗ FAIL — ${data.triggered_count} finding${data.triggered_count !== 1 ? 's' : ''} at or above ${data.fail_on}`
                : `✓ PASS — no findings at or above ${data.fail_on}`}
            </div>
          )}
          <div className="flex items-center gap-2 flex-wrap">
            <span className={clsx(
              'text-sm font-mono font-semibold',
              total === 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
            )}>
              {total === 0 ? '✓ Clean' : `⚠ ${total} finding${total !== 1 ? 's' : ''}`}
            </span>
            {(['critical', 'high', 'medium', 'low', 'info'] as const).map(s =>
              (bySev[s] ?? 0) > 0
                ? (
                  <span
                    key={s}
                    className={`inline-block font-mono text-[9px] uppercase px-1.5 py-px rounded border ${SEVERITY_STYLE[s] ?? SEVERITY_STYLE.info}`}
                  >
                    {s} ×{bySev[s]}
                  </span>
                )
                : null
            )}
          </div>
          {data.findings.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono text-[var(--color-text-muted)]">
                <thead>
                  <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                    <th className="text-left pb-1.5 pr-3">rule_id</th>
                    <th className="text-left pb-1.5 pr-3">severity</th>
                    <th className="text-left pb-1.5 pr-3">target_ref</th>
                    <th className="text-left pb-1.5">strategy</th>
                  </tr>
                </thead>
                <tbody>
                  {data.findings.map((f, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)] last:border-0">
                      <td className="py-1.5 pr-3 text-[var(--color-text)]">{f.rule_id}</td>
                      <td className="py-1.5 pr-3"><SeverityBadge severity={f.severity} /></td>
                      <td className="py-1.5 pr-3 break-all">{f.target_ref}</td>
                      <td className="py-1.5">{f.redaction_strategy}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SemanticResultPane({ result }: { result: ReplayResult }) {
  const score = result.similarity_score ?? 0;
  const pct = Math.round(score * 100);
  const tone = pct >= 80 ? 'success' : pct >= 50 ? 'pending' : 'failure';
  const toneColor = tone === 'success'
    ? 'text-[var(--color-status-success)]'
    : tone === 'pending'
    ? 'text-[var(--color-status-pending)]'
    : 'text-[var(--color-status-failure)]';
  const toneBorder = tone === 'success'
    ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]'
    : tone === 'pending'
    ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
    : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]';

  return (
    <div className="space-y-4">
      <div className={clsx('rounded-lg border p-4', toneBorder)}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={clsx('text-2xl font-mono font-bold', toneColor)}>{pct}%</span>
            <span className="text-xs text-[var(--color-text-faint)] font-mono uppercase tracking-wider">semantic similarity</span>
          </div>
          <span className="text-xs text-[var(--color-text-faint)] font-mono">{result.duration_ms} ms</span>
        </div>
        <p className="mt-2 text-xs text-[var(--color-text-muted)]">
          Average pairwise similarity across {result.model_calls_mocked} model call response{result.model_calls_mocked === 1 ? '' : 's'}.
          {pct >= 80 ? ' High consistency — outputs are semantically stable.'
            : pct >= 50 ? ' Moderate variation — some responses differ in meaning.'
            : ' High variation — outputs diverge significantly across calls.'}
        </p>
        {result.matched_run_id && (
          <p className="mt-1 text-[10px] font-mono text-[var(--color-text-faint)] break-all">
            matched_run_id: {result.matched_run_id}
          </p>
        )}
      </div>

      {/* Similarity gauge */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-3">Similarity gauge</h4>
        <div className="relative h-3 rounded-full bg-[var(--color-bg-sunken)] overflow-hidden">
          <div
            className={clsx('h-full rounded-full transition-all', tone === 'success' ? 'bg-[var(--color-status-success)]' : tone === 'pending' ? 'bg-[var(--color-status-pending)]' : 'bg-[var(--color-status-failure)]')}
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className="flex justify-between mt-1 text-[9px] font-mono text-[var(--color-text-faint)]">
          <span>0%</span><span>50%</span><span>100%</span>
        </div>
      </div>

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Replay metadata</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">replay_id</dt>
          <dd className="text-[var(--color-text-muted)] break-all">{result.replay_id}</dd>
          <dt className="text-[var(--color-text-faint)]">start</dt>
          <dd className="text-[var(--color-text-muted)]">{result.start_time}</dd>
          <dt className="text-[var(--color-text-faint)]">model_calls</dt>
          <dd className="text-[var(--color-text-muted)]">{result.model_calls_mocked}</dd>
        </dl>
      </div>

      {result.env_warnings.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-2">Environment warnings</h4>
          <ul className="space-y-1 text-xs font-mono text-[var(--color-text-muted)]">
            {result.env_warnings.map((w, i) => (
              <li key={i}><span className="text-[var(--color-status-pending)]">⚠ </span>{w.message ?? JSON.stringify(w)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ExactResultPane({ result }: { result: ReplayResult }) {
  const eligible = result.exact_eligible ?? false;
  const reasons = result.exact_reasons ?? [];

  return (
    <div className="space-y-4">
      <div className={clsx(
        'rounded-lg border p-4',
        eligible
          ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]'
          : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]',
      )}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={clsx('text-lg font-mono font-medium', eligible ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
              {eligible ? '✓ eligible' : '✗ not eligible'}
            </span>
            <span className="text-xs text-[var(--color-text-faint)] font-mono uppercase tracking-wider">exact replay</span>
          </div>
          <span className="text-xs text-[var(--color-text-faint)] font-mono">{result.duration_ms} ms</span>
        </div>
        <p className="mt-2 text-xs text-[var(--color-text-muted)]">
          {eligible
            ? 'This capsule meets all requirements for exact replay: deterministic environment lock and seeded model calls.'
            : `This capsule cannot be replayed exactly. ${reasons.length} issue${reasons.length === 1 ? '' : 's'} found.`}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 text-center">
          <span className="block font-mono text-xl text-[var(--color-text)]">{result.exact_hash_count ?? 0}</span>
          <span className="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">inputs hashed</span>
        </div>
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 text-center">
          <span className={clsx('block font-mono text-xl', reasons.length === 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {reasons.length}
          </span>
          <span className="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">blockers</span>
        </div>
      </div>

      {reasons.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-failure)] font-mono mb-2">
            Blockers ({reasons.length})
          </h4>
          <ul className="space-y-2 text-xs font-mono text-[var(--color-text-muted)]">
            {reasons.map((r, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-[var(--color-status-failure)] shrink-0">✗</span>
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Replay metadata</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">replay_id</dt>
          <dd className="text-[var(--color-text-muted)] break-all">{result.replay_id}</dd>
          <dt className="text-[var(--color-text-faint)]">start</dt>
          <dd className="text-[var(--color-text-muted)]">{result.start_time}</dd>
          <dt className="text-[var(--color-text-faint)]">model_calls</dt>
          <dd className="text-[var(--color-text-muted)]">{result.model_calls_mocked}</dd>
        </dl>
      </div>

      {result.env_warnings.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-2">Environment warnings</h4>
          <ul className="space-y-1 text-xs font-mono text-[var(--color-text-muted)]">
            {result.env_warnings.map((w, i) => (
              <li key={i}><span className="text-[var(--color-status-pending)]">⚠ </span>{w.message ?? JSON.stringify(w)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
