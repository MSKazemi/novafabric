import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import type { AssetSummary, AssetDiffResult, RegistrationSuggestion } from '../../../lib/api';
import type { AssetRecord, EvalResult } from '../../../lib/fixtures';
import type { AssetDetail } from '../../../lib/api';
import EvalSparkline, { type EvalHistoryEntry } from '../EvalSparkline';
import { relativeTime } from '../../../lib/time';
import RegistryBrowser from '../../registry/RegistryBrowser';
import ConfirmDialog from '../ConfirmDialog';
import { ErrorBox, Loading } from '../helpers';
import type { Tab } from '../Sidebar';
import { writeResumeItem } from './HomeTab';

function nextStatusFor(current: string): string {
  if (current === 'development') return 'staging';
  if (current === 'staging') return 'production';
  if (current === 'production') return 'archived';
  return 'staging';
}

function validTargetsFor(current: string): string[] {
  if (current === 'development') return ['staging', 'archived'];
  if (current === 'staging') return ['production', 'archived'];
  if (current === 'production') return ['archived'];
  if (current === 'archived') return ['staging'];
  return ['staging'];
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    development: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]',
    staging: 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_12%,transparent)]',
    production: 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)]',
    archived: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)] opacity-60',
  };
  return (
    <span className={clsx('text-[9px] uppercase tracking-wider px-1.5 py-px rounded font-medium', colors[status] ?? colors.development)}>
      {status}
    </span>
  );
}

export default function RegistryTab({
  onFlash,
  refreshTick,
  onCountChange,
  onNavigate,
}: {
  onFlash: (tone: 'success' | 'error', text: string) => void;
  refreshTick: number;
  onCountChange?: (n: number) => void;
  onNavigate?: (tab: Tab) => void;
}) {
  const [assets, setAssets] = useState<AssetSummary[] | null>(null);
  const [detailMap, setDetailMap] = useState<Map<string, AssetDetail>>(new Map());
  const [evalResults, setEvalResults] = useState<EvalResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'development' | 'staging' | 'production' | 'archived'>('all');

  // Sparkline history cache: assetId → history array. Populated lazily by IntersectionObserver.
  const [sparklineHistory, setSparklineHistory] = useState<Record<string, EvalHistoryEntry[]>>({});
  // Tracks which asset IDs have been fetched. useRef to avoid re-render loops inside observer callback.
  const fetchedIds = useRef<Set<string>>(new Set());
  const observerRef = useRef<IntersectionObserver | null>(null);

  // Eval dialog history (20 results for the selected asset).
  const [evalHistory, setEvalHistory] = useState<EvalHistoryEntry[] | null>(null);
  const [evalHistoryLoading, setEvalHistoryLoading] = useState(false);

  // Dialog state
  const [registerOpen, setRegisterOpen] = useState(false);
  const [registerYaml, setRegisterYaml] = useState('');
  const [registerBusy, setRegisterBusy] = useState(false);
  const [evalTarget, setEvalTarget] = useState<AssetSummary | null>(null);
  const [evalBusy, setEvalBusy] = useState(false);
  const [promoteTarget, setPromoteTarget] = useState<AssetSummary | null>(null);
  const [promoteTo, setPromoteTo] = useState('staging');
  const [promoteForce, setPromoteForce] = useState(false);
  const [promoteBusy, setPromoteBusy] = useState(false);
  const [promoteError, setPromoteError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  // C-2 — suggestion engine (empty-state list + register-modal quick-picks)
  const [suggestions, setSuggestions] = useState<RegistrationSuggestion[] | null>(null);
  const [suggestionDraftOpen, setSuggestionDraftOpen] = useState<RegistrationSuggestion | null>(null);
  const [suggestionRegisterBusy, setSuggestionRegisterBusy] = useState(false);
  // Quick-pick suggestions shown inside the Register dialog
  const [registerSuggestions, setRegisterSuggestions] = useState<RegistrationSuggestion[]>([]);

  // DD-3: Rollback
  const [rollbackTarget, setRollbackTarget] = useState<AssetSummary | null>(null);
  const [rollbackReason, setRollbackReason] = useState('');
  const [rollbackBusy, setRollbackBusy] = useState(false);

  // DD-4: Approvals
  type ApprovalRecord = { role: string; actor: string; note: string; approved_at: string };
  const [approvalAsset, setApprovalAsset] = useState<AssetSummary | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [approvalsRequired, setApprovalsRequired] = useState(1);
  const [approvalsSupported, setApprovalsSupported] = useState(true);
  const [approvalsLoading, setApprovalsLoading] = useState(false);
  const [approveRole, setApproveRole] = useState('reviewer');
  const [approveNote, setApproveNote] = useState('');
  const [approveBusy, setApproveBusy] = useState(false);

  // Unregister (v0.20.0)
  const [unregisterTarget, setUnregisterTarget] = useState<AssetSummary | null>(null);
  const [unregisterForce, setUnregisterForce] = useState(false);
  const [unregisterBusy, setUnregisterBusy] = useState(false);

  // Compare versions (DC-6)
  const [compareAssetName, setCompareAssetName] = useState<string | null>(null);
  const [compareFromVersion, setCompareFromVersion] = useState('');
  const [compareToVersion, setCompareToVersion] = useState('');
  const [compareBusy, setCompareBusy] = useState(false);
  const [compareResult, setCompareResult] = useState<AssetDiffResult | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);

  // Pagination state (load-more pattern)
  const PAGE_SIZE = 50;
  const [loadedOffset, setLoadedOffset] = useState(0);
  const [totalAssets, setTotalAssets] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const onCountChangeRef = useRef(onCountChange);
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  const _loadPage = useCallback(async (offset: number, replace: boolean) => {
    try {
      const r = await api.listAssets({ limit: PAGE_SIZE, offset });
      if (replace) {
        setAssets(r.assets);
        setSelectedIds(new Set());
      } else {
        setAssets(prev => [...(prev ?? []), ...r.assets]);
      }
      setTotalAssets(r.total);
      setHasMore(r.has_more);
      setLoadedOffset(offset + r.assets.length);
      onCountChangeRef.current?.(r.total);
      // Load details for newly-fetched assets
      const details = await Promise.all(r.assets.map(a => api.getAsset(a.id).catch(() => null)));
      setDetailMap(prev => {
        const next = new Map(prev);
        for (let i = 0; i < r.assets.length; i++) {
          const d = details[i];
          if (d) next.set(r.assets[i].id, d);
        }
        return next;
      });
      setEvalResults(prev => {
        const flat = replace ? [] : [...prev];
        for (let i = 0; i < r.assets.length; i++) {
          const d = details[i];
          if (!d) continue;
          for (const e of d.eval_results ?? []) {
            let score: number | null = null;
            try {
              const parsed = typeof e.score === 'string' ? JSON.parse(e.score) : e.score;
              if (typeof parsed === 'number') score = parsed;
              else if (parsed && typeof parsed === 'object' && 'score' in parsed) {
                const raw = (parsed as { score: unknown }).score;
                score = typeof raw === 'number' ? raw : null;
              }
            } catch { /* ignore */ }
            flat.push({ asset: `${r.assets[i].name}@${r.assets[i].version}`, suite: e.suite_name, passed: e.passed, score });
          }
        }
        return flat;
      });
    } catch (e) {
      setError((e as Error).message);
    }
  }, []); // onCountChange intentionally excluded — accessed via ref

  const refresh = useCallback(async () => {
    setError(null);
    setLoadedOffset(0);
    await _loadPage(0, true);
  }, [_loadPage]);

  const loadMore = useCallback(async () => {
    if (!hasMore || loadingMore) return;
    setLoadingMore(true);
    try {
      await _loadPage(loadedOffset, false);
    } finally {
      setLoadingMore(false);
    }
  }, [hasMore, loadingMore, _loadPage, loadedOffset]);

  useEffect(() => { refresh(); }, [refresh, refreshTick]);

  // Fetch suggestions whenever assets list changes
  useEffect(() => {
    if (assets === null) return;
    api.getSuggestions(10)
      .then((r) => setSuggestions(r.suggestions))
      .catch(() => setSuggestions([]));
  }, [assets]);

  // Fetch quick-pick suggestions each time the register modal opens
  useEffect(() => {
    if (!registerOpen) { setRegisterSuggestions([]); return; }
    api.getSuggestions(8)
      .then((r) => setRegisterSuggestions(r.suggestions))
      .catch(() => setRegisterSuggestions([]));
  }, [registerOpen]);

  // IntersectionObserver callback ref for sparkline lazy loading
  const rowRefCallback = useCallback((el: HTMLTableCellElement | null, assetId: string) => {
    if (!el) return;
    if (!observerRef.current) {
      observerRef.current = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            const id = (entry.target as HTMLElement).dataset.assetId;
            if (!id || fetchedIds.current.has(id)) continue;
            fetchedIds.current.add(id);
            api.getEvalHistory(id, 10)
              .then((data) => {
                setSparklineHistory((prev) => ({ ...prev, [id]: data.history as EvalHistoryEntry[] }));
              })
              .catch(() => { /* leave empty — sparkline renders null */ });
          }
        },
        { threshold: 0.1 },
      );
    }
    el.dataset.assetId = assetId;
    observerRef.current.observe(el);
  }, []);

  // Disconnect observer on unmount
  useEffect(() => () => { observerRef.current?.disconnect(); }, []);

  // Fetch eval history when the eval dialog opens
  useEffect(() => {
    if (!evalTarget) {
      setEvalHistory(null);
      return;
    }
    setEvalHistoryLoading(true);
    api.getEvalHistory(evalTarget.id, 20)
      .then((data) => setEvalHistory(data.history as EvalHistoryEntry[]))
      .catch(() => setEvalHistory([]))
      .finally(() => setEvalHistoryLoading(false));
  }, [evalTarget]);

  const onRegisterConfirm = useCallback(async () => {
    setRegisterBusy(true);
    try {
      const res = await api.registerAsset(registerYaml);
      onFlash('success', `Registered ${res.asset.name}@${res.asset.version}`);
      setRegisterOpen(false);
      setRegisterYaml('');
      await refresh();
    } catch (e) {
      onFlash('error', `Register failed: ${(e as Error).message}`);
    } finally { setRegisterBusy(false); }
  }, [registerYaml, onFlash, refresh]);

  const onEvalConfirm = useCallback(async () => {
    if (!evalTarget) return;
    setEvalBusy(true);
    const targetId = evalTarget.id;
    try {
      const res = await api.evalAsset(evalTarget.id);
      const passed = res.result?.passed;
      onFlash(passed ? 'success' : 'error', `Eval ${evalTarget.name}@${evalTarget.version}: ${passed ? 'PASS' : 'FAIL'}`);
      setEvalTarget(null);
      // Re-fetch sparkline immediately — IntersectionObserver won't re-fire for
      // a row that's already visible, so we can't rely on it to update the bars.
      fetchedIds.current.delete(targetId);
      api.getEvalHistory(targetId, 10)
        .then((data) => {
          setSparklineHistory((prev) => ({ ...prev, [targetId]: data.history as EvalHistoryEntry[] }));
        })
        .catch(() => { /* leave stale on error */ });
      await refresh();
    } catch (e) {
      onFlash('error', `Eval failed: ${(e as Error).message}`);
    } finally { setEvalBusy(false); }
  }, [evalTarget, onFlash, refresh]);

  const onPromoteConfirm = useCallback(async () => {
    if (!promoteTarget) return;
    setPromoteBusy(true);
    try {
      const res = await api.promoteAsset(promoteTarget.id, promoteTo, promoteForce);
      onFlash('success', `${res.asset.name}@${res.asset.version} → ${promoteTo}${promoteForce ? ' (forced)' : ''}`);
      setPromoteTarget(null);
      setPromoteForce(false);
      setPromoteError(null);
      await refresh();
    } catch (e) {
      const msg = (e as Error).message;
      onFlash('error', `Promote failed: ${msg}`);
      setPromoteError(msg);
    } finally { setPromoteBusy(false); }
  }, [promoteTarget, promoteTo, promoteForce, onFlash, refresh]);

  const onRollbackConfirm = useCallback(async () => {
    if (!rollbackTarget) return;
    setRollbackBusy(true);
    try {
      const res = await api.rollbackAsset(rollbackTarget.name, rollbackReason);
      onFlash('success', `Rolled back ${rollbackTarget.name}: ${res.result.archived_version} → ${res.result.restored_version}`);
      setRollbackTarget(null);
      setRollbackReason('');
      await refresh();
    } catch (e) {
      onFlash('error', `Rollback failed: ${(e as Error).message}`);
    } finally { setRollbackBusy(false); }
  }, [rollbackTarget, rollbackReason, onFlash, refresh]);

  const onUnregisterConfirm = useCallback(async () => {
    if (!unregisterTarget) return;
    setUnregisterBusy(true);
    try {
      await api.unregisterAsset(unregisterTarget.name, unregisterTarget.version, unregisterForce);
      onFlash('success', `Unregistered ${unregisterTarget.name}@${unregisterTarget.version}`);
      setUnregisterTarget(null);
      setUnregisterForce(false);
      await refresh();
    } catch (e) {
      onFlash('error', `Unregister failed: ${(e as Error).message}`);
    } finally { setUnregisterBusy(false); }
  }, [unregisterTarget, unregisterForce, onFlash, refresh]);

  const onApprovalOpen = useCallback(async (a: AssetSummary) => {
    setApprovalAsset(a);
    setApprovals([]);
    setApprovalsLoading(true);
    try {
      const res = await api.getApprovals(a.id);
      setApprovals(res.approvals);
      setApprovalsRequired(res.required);
      setApprovalsSupported(res.supported);
    } catch { setApprovals([]); }
    finally { setApprovalsLoading(false); }
  }, []);

  const onApproveSubmit = useCallback(async () => {
    if (!approvalAsset) return;
    setApproveBusy(true);
    try {
      await api.approveAsset(approvalAsset.id, approveRole, approveNote);
      onFlash('success', `Approval recorded for ${approvalAsset.name}@${approvalAsset.version}`);
      // Refresh approvals inline
      const res = await api.getApprovals(approvalAsset.id);
      setApprovals(res.approvals);
      setApproveNote('');
    } catch (e) {
      onFlash('error', `Approval failed: ${(e as Error).message}`);
    } finally { setApproveBusy(false); }
  }, [approvalAsset, approveRole, approveNote, onFlash]);

  const onBulkPromote = useCallback(async () => {
    setBulkBusy(true);
    let ok = 0; let fail = 0;
    for (const id of selectedIds) {
      const asset = assets?.find(a => a.id === id);
      if (!asset) continue;
      const target = nextStatusFor(asset.status);
      try {
        await api.promoteAsset(id, target, false);
        ok++;
      } catch { fail++; }
    }
    setBulkBusy(false);
    setSelectedIds(new Set());
    onFlash(fail === 0 ? 'success' : 'error', `Promoted ${ok}${fail > 0 ? `, ${fail} failed` : ''}`);
    await refresh();
  }, [selectedIds, assets, onFlash, refresh]);

  // Build a map of assetName → sorted versions (from all assets, not just visible).
  // Computed here (before early returns) so the useCallback hooks below can reference it.
  const versionsByName = (assets ?? []).reduce<Record<string, string[]>>((acc, a) => {
    if (!acc[a.name]) acc[a.name] = [];
    if (!acc[a.name].includes(a.version)) acc[a.name].push(a.version);
    return acc;
  }, {});

  // Virtual scroll — BL-5: computed before early returns so useVirtualizer hook is unconditional
  const tableContainerRef = useRef<HTMLDivElement>(null);
  const visibleAssetsMemo = useMemo(() => {
    if (!assets) return [];
    return assets.filter(a => {
      if (statusFilter !== 'all' && a.status !== statusFilter) return false;
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return a.name.toLowerCase().includes(q) || a.asset_type.toLowerCase().includes(q) || a.version.toLowerCase().includes(q);
    });
  }, [assets, statusFilter, search]);

  const rowVirtualizer = useVirtualizer({
    count: visibleAssetsMemo.length,
    getScrollElement: () => tableContainerRef.current,
    estimateSize: () => 40,
    overscan: 10,
  });

  // These callbacks must be defined before any early return (React rules of hooks).
  const onCompareOpen = useCallback((assetName: string) => {
    const versions = versionsByName[assetName] ?? [];
    // Default: from = second-latest, to = latest (versions are newest-first from API)
    setCompareAssetName(assetName);
    setCompareFromVersion(versions.length >= 2 ? versions[1] : versions[0] ?? '');
    setCompareToVersion(versions[0] ?? '');
    setCompareResult(null);
    setCompareError(null);
  }, [versionsByName]);

  const onCompareSubmit = useCallback(async () => {
    if (!compareAssetName || !compareFromVersion || !compareToVersion) return;
    setCompareBusy(true);
    setCompareResult(null);
    setCompareError(null);
    try {
      const result = await api.getAssetDiff(compareAssetName, compareFromVersion, compareToVersion);
      setCompareResult(result);
    } catch (e) {
      setCompareError((e as Error).message);
    } finally {
      setCompareBusy(false);
    }
  }, [compareAssetName, compareFromVersion, compareToVersion]);

  if (error && !assets) return <ErrorBox message={error} onRetry={refresh} />;
  if (!assets) return <Loading />;

  // After early returns, assets is non-null — use the memo-computed list
  const visibleAssets = visibleAssetsMemo;

  const adapted: AssetRecord[] = visibleAssets.map(a => {
    const detail = detailMap.get(a.id);
    let spec: Record<string, unknown> = {};
    if (detail?.spec_json) {
      try { spec = JSON.parse(detail.spec_json as string); } catch { /* ignore malformed JSON */ }
    }
    return {
      name: a.name,
      version: a.version,
      asset_type: a.asset_type as AssetRecord['asset_type'],
      status: (a.status === 'production' || a.status === 'promoted' ? 'promoted' : 'development'),
      description: (spec.description as string) ?? '',
      spec,
    };
  });

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--color-text-muted)]">
        <span>
          {visibleAssets.length}{visibleAssets.length !== assets.length ? ` / ${assets.length}` : ''} asset{visibleAssets.length === 1 ? '' : 's'} · {evalResults.length} eval result{evalResults.length === 1 ? '' : 's'}
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setRegisterOpen(true)}
            className="px-3 py-1.5 rounded-md text-xs font-medium border border-[var(--color-border)] hover:border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] hover:bg-[var(--color-bg)] text-[var(--color-text)]"
          >
            + Register asset
          </button>
          {onNavigate && (
            <button
              onClick={() => onNavigate('commands')}
              className="text-[10px] text-[var(--color-accent)] hover:underline"
            >
              ⎘ Register via CLI
            </button>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2">
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search name, type, version…"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <select
          value={statusFilter}
          onChange={e => setStatusFilter(e.target.value as typeof statusFilter)}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-1.5 font-mono text-xs"
        >
          <option value="all">all status</option>
          <option value="development">development</option>
          <option value="staging">staging</option>
          <option value="production">production</option>
          <option value="archived">archived</option>
        </select>
      </div>

      {adapted.length === 0 ? (
        <SuggestionsEmptyState
          suggestions={suggestions}
          onDraftOpen={setSuggestionDraftOpen}
          onRegisterOpen={() => setRegisterOpen(true)}
        />
      ) : (
        <>
          <RegistryBrowser assets={adapted} evalResults={evalResults} />
          {suggestions !== null && suggestions.length > 0 && (
            <SuggestionsBanner
              suggestions={suggestions}
              onDraftOpen={setSuggestionDraftOpen}
            />
          )}
        </>
      )}

      {/* Lifecycle action table — BL-5: virtual scroll to collapse DOM at 10K rows */}
      {visibleAssets.length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
          <table className="w-full text-xs" style={{ tableLayout: 'fixed' }}>
            <thead className="border-b border-[var(--color-border)] bg-[var(--color-bg-sunken)]">
              <tr>
                <th className="w-8 px-2">
                  <input
                    type="checkbox"
                    checked={visibleAssets.length > 0 && visibleAssets.every(a => selectedIds.has(a.id))}
                    onChange={e => {
                      if (e.target.checked) setSelectedIds(new Set(visibleAssets.map(a => a.id)));
                      else setSelectedIds(new Set());
                    }}
                    className="accent-[var(--color-accent)]"
                  />
                </th>
                <th className="text-left px-4 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Asset</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Type</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Status</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Trend</th>
                <th className="text-right px-4 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Actions</th>
              </tr>
            </thead>
          </table>
          <div
            ref={tableContainerRef}
            style={{ overflowY: 'auto', maxHeight: 600 }}
          >
            <table className="w-full text-xs" style={{ tableLayout: 'fixed' }}>
              <tbody style={{ display: 'block', position: 'relative', height: rowVirtualizer.getTotalSize() }}>
                {rowVirtualizer.getVirtualItems().map(virtualRow => {
                  const a = visibleAssets[virtualRow.index];
                  if (!a) return null;
                  return (
                    <tr
                      key={a.id}
                      data-index={virtualRow.index}
                      ref={rowVirtualizer.measureElement}
                      style={{ display: 'table', width: '100%', tableLayout: 'fixed', position: 'absolute', top: 0, left: 0, transform: `translateY(${virtualRow.start}px)` }}
                      className="hover:bg-[var(--color-bg-sunken)] transition-colors border-b border-[var(--color-border)]"
                    >
                      <td className="px-2 w-8">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(a.id)}
                          onChange={e => {
                            const next = new Set(selectedIds);
                            if (e.target.checked) next.add(a.id); else next.delete(a.id);
                            setSelectedIds(next);
                          }}
                          className="accent-[var(--color-accent)]"
                        />
                      </td>
                      <td className="px-4 py-2.5 font-mono">
                        <span className="text-[var(--color-text)]">{a.name}</span>
                        <span className="text-[var(--color-text-faint)]">@{a.version}</span>
                      </td>
                      <td className="px-3 py-2.5 text-[var(--color-text-muted)] font-mono text-[10px]">{a.asset_type}</td>
                      <td className="px-3 py-2.5">
                        <StatusBadge status={a.status} />
                      </td>
                      <td
                        className="px-3 py-2.5"
                        ref={(el) => rowRefCallback(el, a.id)}
                      >
                        <EvalSparkline history={sparklineHistory[a.id] ?? []} maxBars={10} />
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <div className="inline-flex items-center gap-1">
                          <button
                            onClick={() => {
                              setEvalTarget(a);
                              writeResumeItem({
                                tab: 'registry',
                                label: `Registry · ${a.name}@${a.version}`,
                                meta: `${a.status} · ${a.asset_type}`,
                                icon: '◈',
                              });
                            }}
                            title="Run eval suites"
                            className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-border-strong)] hover:text-[var(--color-text)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                          >eval</button>
                          <button
                            onClick={() => { setPromoteTarget(a); setPromoteTo(nextStatusFor(a.status)); setPromoteError(null); }}
                            title="Promote to next lifecycle stage"
                            className="px-2 py-1 rounded border border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                          >promote →</button>
                          {(versionsByName[a.name]?.length ?? 0) >= 2 && (
                            <button
                              onClick={() => onCompareOpen(a.name)}
                              title="Compare two versions of this asset"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-border-strong)] hover:text-[var(--color-text)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >compare…</button>
                          )}
                          {a.status === 'production' && (
                            <button
                              onClick={() => { setRollbackTarget(a); setRollbackReason(''); }}
                              title="Roll back to previous production version"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-status-failure)] hover:text-[var(--color-status-failure)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >rollback</button>
                          )}
                          {(a.status === 'staging' || a.status === 'pending_approval') && (
                            <button
                              onClick={() => onApprovalOpen(a)}
                              title="View and record approvals"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >approve…</button>
                          )}
                          {(a.status === 'development' || a.status === 'archived') && (
                            <button
                              onClick={() => { setUnregisterTarget(a); setUnregisterForce(false); }}
                              title="Permanently remove this asset from the registry"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-status-failure)] hover:text-[var(--color-status-failure)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >delete</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {selectedIds.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-4 py-2 shadow-lg text-sm">
          <span className="text-[var(--color-text-muted)] text-xs">{selectedIds.size} selected</span>
          <button
            disabled={bulkBusy}
            onClick={onBulkPromote}
            className="px-3 py-1 rounded-full bg-[var(--color-accent)] text-[var(--color-accent-fg)] text-xs font-medium hover:opacity-90 disabled:opacity-50"
          >
            {bulkBusy ? 'Promoting…' : `Promote ${selectedIds.size}`}
          </button>
          <button
            onClick={() => setSelectedIds(new Set())}
            className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-xs"
          >Deselect all</button>
        </div>
      )}

      {/* Dialogs */}
      <ConfirmDialog
        open={registerOpen}
        title="Register a new asset"
        description="Register a model, prompt, tool, dataset, agent, or evaluation suite from a YAML spec. The asset starts in `development` and must pass eval before promotion."
        cliEquivalent="nova register <spec.yaml>"
        size="lg"
        details={
          <div className="space-y-3">
            {registerSuggestions.length > 0 && (
              <div>
                <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">
                  Detected from recent runs — click to pre-fill
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {registerSuggestions.map((s, i) => (
                    <button
                      key={i}
                      type="button"
                      onClick={() => setRegisterYaml(s.draft_spec_yaml)}
                      title={`${Math.round(s.confidence * 100)}% confidence · ${s.call_count} calls`}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] font-mono transition-colors"
                    >
                      <span className="text-[var(--color-text-faint)] uppercase tracking-wider">{s.asset_type}</span>
                      <span className="text-[var(--color-text)]">{s.detected_name}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            <label className="block">
              <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">YAML spec</span>
              <textarea
                value={registerYaml}
                onChange={e => setRegisterYaml(e.target.value)}
                placeholder={`name: my-prompt\nversion: 0.1.0\nasset_type: prompt\n...`}
                rows={10}
                className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-xs focus:border-[var(--color-accent)] focus:outline-none"
              />
            </label>
          </div>
        }
        confirmLabel="Register"
        busy={registerBusy}
        onConfirm={onRegisterConfirm}
        onCancel={() => setRegisterOpen(false)}
      />

      <ConfirmDialog
        open={!!evalTarget}
        title="Run evaluation suites"
        description={evalTarget ? `Run all evaluation suites configured for this asset. Results are written to the registry and used to gate promotion.` : ''}
        cliEquivalent={evalTarget ? `nova eval ${evalTarget.name}@${evalTarget.version}` : ''}
        details={evalTarget && (
          <div className="mt-3 space-y-1.5">
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-semibold">
              Eval history
            </span>
            {evalHistoryLoading && (
              <p className="text-[10px] text-[var(--color-text-faint)]">Loading history…</p>
            )}
            {!evalHistoryLoading && evalHistory !== null && evalHistory.length === 0 && (
              <p className="text-[10px] text-[var(--color-text-faint)]">
                No eval runs recorded yet for this asset.
              </p>
            )}
            {!evalHistoryLoading && evalHistory && evalHistory.length > 0 && (
              <EvalHistoryChart history={evalHistory} />
            )}
          </div>
        )}
        confirmLabel="Run eval"
        busy={evalBusy}
        onConfirm={onEvalConfirm}
        onCancel={() => setEvalTarget(null)}
      />

      {/* Compare versions dialog (DC-6) */}
      {compareAssetName !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-lg rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-xl p-5 space-y-4">
            <div className="flex items-start justify-between">
              <div>
                <h2 className="text-sm font-semibold text-[var(--color-text)]">Compare versions — {compareAssetName}</h2>
                <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">CLI equivalent: <code className="font-mono">nova diff {compareAssetName}@{compareFromVersion} {compareAssetName}@{compareToVersion}</code></p>
              </div>
              <button
                onClick={() => { setCompareAssetName(null); setCompareResult(null); setCompareError(null); }}
                className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none"
                aria-label="Close"
              >×</button>
            </div>

            {/* Version selectors */}
            <div className="flex items-center gap-3">
              <label className="flex-1 block">
                <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">From</span>
                <select
                  value={compareFromVersion}
                  onChange={e => { setCompareFromVersion(e.target.value); setCompareResult(null); setCompareError(null); }}
                  className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
                >
                  {(versionsByName[compareAssetName] ?? []).map(v => (
                    <option key={v} value={v}>{v}</option>
                  ))}
                </select>
              </label>
              <span className="text-[var(--color-text-faint)] mt-4">→</span>
              <label className="flex-1 block">
                <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">To</span>
                <select
                  value={compareToVersion}
                  onChange={e => { setCompareToVersion(e.target.value); setCompareResult(null); setCompareError(null); }}
                  className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
                >
                  {(versionsByName[compareAssetName] ?? []).map(v => (
                    <option key={v} value={v}>{v}</option>
                  ))}
                </select>
              </label>
            </div>

            <button
              onClick={onCompareSubmit}
              disabled={compareBusy || compareFromVersion === compareToVersion}
              className="w-full py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {compareBusy ? 'Comparing…' : 'Compare'}
            </button>

            {/* Error */}
            {compareError && (
              <p className="text-[11px] text-[var(--color-status-failure)]">{compareError}</p>
            )}

            {/* Diff result */}
            {compareResult && (
              <div className="space-y-2">
                {compareResult.identical ? (
                  <p className="text-[11px] text-[var(--color-status-success)] font-medium">No differences — specs are identical.</p>
                ) : (
                  <div className="rounded border border-[var(--color-border)] overflow-hidden">
                    <table className="w-full text-[10px] font-mono">
                      <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)]">
                        <tr>
                          <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Key</th>
                          <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">From ({compareFromVersion})</th>
                          <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">To ({compareToVersion})</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--color-border)]">
                        {compareResult.changed.map(r => (
                          <tr key={`chg-${r.key}`} className="bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]">
                            <td className="px-3 py-1.5 text-[var(--color-text-muted)]">
                              <span className="text-[var(--color-status-pending)] mr-1">~</span>{r.key}
                            </td>
                            <td className="px-3 py-1.5 text-[var(--color-status-failure)] line-through opacity-70">{JSON.stringify(r.from)}</td>
                            <td className="px-3 py-1.5 text-[var(--color-status-success)]">{JSON.stringify(r.to)}</td>
                          </tr>
                        ))}
                        {compareResult.added.map(r => (
                          <tr key={`add-${r.key}`} className="bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]">
                            <td className="px-3 py-1.5 text-[var(--color-text-muted)]">
                              <span className="text-[var(--color-status-success)] mr-1">+</span>{r.key}
                            </td>
                            <td className="px-3 py-1.5 text-[var(--color-text-faint)]">—</td>
                            <td className="px-3 py-1.5 text-[var(--color-status-success)]">{JSON.stringify(r.value)}</td>
                          </tr>
                        ))}
                        {compareResult.removed.map(r => (
                          <tr key={`rem-${r.key}`} className="bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]">
                            <td className="px-3 py-1.5 text-[var(--color-text-muted)]">
                              <span className="text-[var(--color-status-failure)] mr-1">-</span>{r.key}
                            </td>
                            <td className="px-3 py-1.5 text-[var(--color-status-failure)]">{JSON.stringify(r.value)}</td>
                            <td className="px-3 py-1.5 text-[var(--color-text-faint)]">—</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Load more */}
      {hasMore && (
        <div className="flex gap-2 items-center justify-center text-[10px] py-1">
          <button
            onClick={loadMore}
            disabled={loadingMore}
            className="px-3 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {loadingMore ? 'Loading…' : `Load more (~${totalAssets - (assets?.length ?? 0)} remaining)`}
          </button>
        </div>
      )}

      {/* Validate spec panel */}
      <ValidateSpecPanel onFlash={onFlash} />

      {/* Suggest register panel */}
      <SuggestRegisterPanel
        suggestions={suggestions}
        onRefresh={() => {
          api.getSuggestions(10)
            .then((r) => setSuggestions(r.suggestions))
            .catch(() => setSuggestions([]));
        }}
      />

      {/* Asset report panel */}
      <ReportPanel onFlash={onFlash} />

      {/* C-2 — Draft spec review modal */}
      {suggestionDraftOpen !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-lg rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-xl p-5 space-y-4">
            <div className="flex items-start justify-between">
              <div>
                <h2 className="text-sm font-semibold text-[var(--color-text)]">
                  Register {suggestionDraftOpen.asset_type}: {suggestionDraftOpen.detected_name}
                </h2>
                <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
                  Review and edit the draft spec, then register.
                </p>
              </div>
              <button
                onClick={() => setSuggestionDraftOpen(null)}
                className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none"
                aria-label="Close"
              >×</button>
            </div>
            {suggestionDraftOpen.warnings.length > 0 && (
              <div className="space-y-1">
                {suggestionDraftOpen.warnings.map((w, i) => (
                  <p key={i} className="text-[11px] text-[var(--color-status-pending)]">⚠ {w}</p>
                ))}
              </div>
            )}
            <label className="block">
              <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">YAML spec</span>
              <textarea
                defaultValue={suggestionDraftOpen.draft_spec_yaml}
                id="suggestion-draft-yaml"
                rows={14}
                className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-xs focus:border-[var(--color-accent)] focus:outline-none"
              />
            </label>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setSuggestionDraftOpen(null)}
                className="px-3 py-1.5 rounded border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              >Cancel</button>
              <button
                disabled={suggestionRegisterBusy}
                onClick={async () => {
                  const textarea = document.getElementById('suggestion-draft-yaml') as HTMLTextAreaElement;
                  const yaml = textarea?.value ?? suggestionDraftOpen.draft_spec_yaml;
                  setSuggestionRegisterBusy(true);
                  try {
                    const res = await api.registerAsset(yaml);
                    onFlash('success', `Registered ${res.asset.name}@${res.asset.version}`);
                    setSuggestionDraftOpen(null);
                    await refresh();
                  } catch (e) {
                    onFlash('error', `Register failed: ${(e as Error).message}`);
                  } finally { setSuggestionRegisterBusy(false); }
                }}
                className="px-3 py-1.5 rounded border border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)] text-xs font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
              >{suggestionRegisterBusy ? 'Registering…' : 'Register'}</button>
            </div>
          </div>
        </div>
      )}

      {/* DD-3: Rollback dialog */}
      <ConfirmDialog
        open={!!rollbackTarget}
        title="Roll back asset"
        description={rollbackTarget ? `Archive the current production version of ${rollbackTarget.name}@${rollbackTarget.version} and restore the most recent previous production version. This action is recorded in the audit trail.` : ''}
        cliEquivalent={rollbackTarget ? `nova rollback ${rollbackTarget.name}` : ''}
        details={rollbackTarget && (
          <label className="block mt-2">
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Reason (optional)</span>
            <input
              type="text"
              value={rollbackReason}
              onChange={e => setRollbackReason(e.target.value)}
              placeholder="e.g. regression in production"
              className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-xs focus:border-[var(--color-accent)] focus:outline-none"
            />
          </label>
        )}
        confirmLabel="Roll back"
        busy={rollbackBusy}
        onConfirm={onRollbackConfirm}
        onCancel={() => { setRollbackTarget(null); setRollbackReason(''); }}
        tone="destructive"
      />

      {/* v0.20.0: Unregister dialog */}
      <ConfirmDialog
        open={!!unregisterTarget}
        title="Delete asset"
        description={unregisterTarget ? `Permanently remove ${unregisterTarget.name}@${unregisterTarget.version} from the registry. This cannot be undone. Only development and archived assets can be deleted without --force.` : ''}
        cliEquivalent={unregisterTarget ? `nova unregister ${unregisterTarget.name}@${unregisterTarget.version}${unregisterForce ? ' --force' : ''}` : ''}
        details={unregisterTarget && (
          <label className="flex items-center gap-2 text-[var(--color-text-muted)] mt-2">
            <input type="checkbox" checked={unregisterForce} onChange={e => setUnregisterForce(e.target.checked)} className="accent-[var(--color-status-failure)]" />
            <span><strong>Force</strong> — bypass status guard (for staging/production/pending_approval). Use with caution.</span>
          </label>
        )}
        confirmLabel="Delete asset"
        busy={unregisterBusy}
        onConfirm={onUnregisterConfirm}
        onCancel={() => { setUnregisterTarget(null); setUnregisterForce(false); }}
        tone="destructive"
      />

      {/* DD-4: Approvals modal */}
      {approvalAsset !== null && approvalsSupported && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-lg rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-xl p-5 space-y-4">
            <div className="flex items-start justify-between">
              <div>
                <h2 className="text-sm font-semibold text-[var(--color-text)]">
                  Approvals — {approvalAsset.name}@{approvalAsset.version}
                </h2>
                <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
                  CLI equivalent: <code className="font-mono">nova approve {approvalAsset.name}@{approvalAsset.version} --role &lt;role&gt;</code>
                </p>
              </div>
              <button
                onClick={() => { setApprovalAsset(null); setApproveNote(''); }}
                className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none"
                aria-label="Close"
              >×</button>
            </div>

            {/* Approval count */}
            <div className="text-xs text-[var(--color-text-muted)]">
              {approvalsLoading
                ? 'Loading approvals…'
                : `${approvals.length} approval${approvals.length === 1 ? '' : 's'} recorded / ${approvalsRequired} required`
              }
            </div>

            {/* Existing approvals list */}
            {!approvalsLoading && approvals.length > 0 && (
              <div className="rounded border border-[var(--color-border)] overflow-hidden">
                <table className="w-full text-[10px] font-mono">
                  <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)]">
                    <tr>
                      <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Role</th>
                      <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Actor</th>
                      <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Note</th>
                      <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">At</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--color-border)]">
                    {approvals.map((ap, i) => (
                      <tr key={i} className="hover:bg-[var(--color-bg-sunken)] transition-colors">
                        <td className="px-3 py-1.5 text-[var(--color-text-muted)] uppercase tracking-wider text-[9px]">{ap.role}</td>
                        <td className="px-3 py-1.5 text-[var(--color-text)]">{ap.actor}</td>
                        <td className="px-3 py-1.5 text-[var(--color-text-faint)]">{ap.note || '—'}</td>
                        <td className="px-3 py-1.5 text-[var(--color-text-faint)]">{ap.approved_at.slice(0, 19).replace('T', ' ')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Add approval form */}
            <div className="space-y-2 pt-1 border-t border-[var(--color-border)]">
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Record approval</p>
              <div className="flex items-center gap-2">
                <label className="flex-1 block">
                  <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Role</span>
                  <select
                    value={approveRole}
                    onChange={e => setApproveRole(e.target.value)}
                    className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
                  >
                    <option value="reviewer">reviewer</option>
                    <option value="security">security</option>
                    <option value="compliance">compliance</option>
                  </select>
                </label>
              </div>
              <label className="block">
                <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Note (optional)</span>
                <textarea
                  value={approveNote}
                  onChange={e => setApproveNote(e.target.value)}
                  placeholder="e.g. reviewed model card, no issues found"
                  rows={2}
                  className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-xs focus:border-[var(--color-accent)] focus:outline-none"
                />
              </label>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => { setApprovalAsset(null); setApproveNote(''); }}
                  className="px-3 py-1.5 rounded border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                >Close</button>
                <button
                  disabled={approveBusy}
                  onClick={onApproveSubmit}
                  className="px-3 py-1.5 rounded border border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)] text-xs font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
                >{approveBusy ? 'Recording…' : 'Record approval'}</button>
              </div>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={!!promoteTarget}
        title="Promote asset"
        description={promoteTarget ? `Move ${promoteTarget.name}@${promoteTarget.version} from ${promoteTarget.status} to a new lifecycle stage.${promoteTarget.asset_type === 'agent' ? ' Agent assets must have a passing eval unless --force is used.' : ''}` : ''}
        cliEquivalent={promoteTarget ? `nova promote ${promoteTarget.name}@${promoteTarget.version} --to ${promoteTo}${promoteForce ? ' --force' : ''}` : ''}
        details={promoteTarget && (() => {
          const validTargets = validTargetsFor(promoteTarget.status);
          return (
            <div className="space-y-2">
              <div className="block">
                <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Target status</span>
                <div className="mt-1 flex rounded border border-[var(--color-border)] overflow-hidden text-xs font-mono">
                  {(['staging', 'production', 'archived'] as const).map((s) => {
                    const isValid = validTargets.includes(s);
                    return (
                      <button
                        key={s}
                        type="button"
                        disabled={!isValid}
                        onClick={() => { setPromoteTo(s); setPromoteError(null); }}
                        className={clsx(
                          'flex-1 py-2 transition-colors',
                          !isValid && 'opacity-30 cursor-not-allowed',
                          isValid && promoteTo === s
                            ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                            : isValid
                              ? 'bg-[var(--color-bg)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]'
                              : 'bg-[var(--color-bg)] text-[var(--color-text-muted)]',
                        )}
                      >{s}</button>
                    );
                  })}
                </div>
              </div>
              {promoteError && (
                <p className="text-[11px] text-[var(--color-status-failure)] rounded bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] px-2 py-1">{promoteError}</p>
              )}
              <label className="flex items-center gap-2 text-[var(--color-text-muted)]">
                <input type="checkbox" checked={promoteForce} onChange={e => setPromoteForce(e.target.checked)} className="accent-[var(--color-status-failure)]" />
                <span><strong>Force</strong> — bypass the eval-passing gate (only for agents). Audit log records this clearly.</span>
              </label>
            </div>
          );
        })()}
        confirmLabel={`Promote → ${promoteTo}`}
        busy={promoteBusy}
        onConfirm={onPromoteConfirm}
        onCancel={() => { setPromoteTarget(null); setPromoteForce(false); setPromoteError(null); }}
        tone={promoteForce ? 'destructive' : 'default'}
      />
    </div>
  );
}

// C-2 — Compact banner shown when there are unregistered detections alongside existing assets.
function SuggestionsBanner({
  suggestions,
  onDraftOpen,
}: {
  suggestions: RegistrationSuggestion[];
  onDraftOpen: (s: RegistrationSuggestion) => void;
}) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-4 py-3 space-y-2">
      <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">
        {suggestions.length} unregistered asset{suggestions.length > 1 ? 's' : ''} detected in recent runs
      </p>
      <div className="flex flex-wrap gap-1.5">
        {suggestions.map((s, i) => (
          <button
            key={i}
            type="button"
            onClick={() => onDraftOpen(s)}
            title={`${Math.round(s.confidence * 100)}% confidence · ${s.call_count} calls`}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] font-mono transition-colors"
          >
            <span className="text-[var(--color-text-faint)] uppercase tracking-wider">{s.asset_type}</span>
            <span className="text-[var(--color-text)]">{s.detected_name}</span>
            <span className="text-[var(--color-text-faint)]">{Math.round(s.confidence * 100)}%</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// C-2 — Smart empty state shown when registry is empty but runs exist.
function SuggestionsEmptyState({
  suggestions,
  onDraftOpen,
  onRegisterOpen,
}: {
  suggestions: RegistrationSuggestion[] | null;
  onDraftOpen: (s: RegistrationSuggestion) => void;
  onRegisterOpen: () => void;
}) {
  const hasSuggestions = suggestions !== null && suggestions.length > 0;
  const byType = suggestions?.reduce<Record<string, number>>((acc, s) => {
    acc[s.asset_type] = (acc[s.asset_type] ?? 0) + 1;
    return acc;
  }, {}) ?? {};
  const typesSummary = Object.entries(byType)
    .map(([t, n]) => `${n} ${t}${n > 1 ? 's' : ''}`)
    .join('  ·  ');

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-8 space-y-4">
      <div className="text-center space-y-1">
        <p className="text-sm text-[var(--color-text-muted)]">No assets registered.</p>
        {hasSuggestions && (
          <p className="text-xs text-[var(--color-text-faint)]">
            {suggestions!.length} unregistered asset{suggestions!.length > 1 ? 's' : ''} detected in recent runs
            {typesSummary ? ` — ${typesSummary}` : ''}
          </p>
        )}
        {suggestions === null && (
          <p className="text-[10px] text-[var(--color-text-faint)] animate-pulse">Scanning recent runs…</p>
        )}
        {suggestions !== null && suggestions.length === 0 && (
          <p className="text-[10px] text-[var(--color-text-faint)]">
            Use Register above or run <code className="font-mono">nova register &lt;spec.yaml&gt;</code>
          </p>
        )}
      </div>

      {hasSuggestions && (
        <div className="rounded border border-[var(--color-border)] overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-[var(--color-bg-raised)] border-b border-[var(--color-border)]">
              <tr>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Type</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Name</th>
                <th className="text-right px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Calls</th>
                <th className="text-right px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Confidence</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {suggestions!.map((s, i) => (
                <tr key={i} className="hover:bg-[var(--color-bg-raised)] transition-colors">
                  <td className="px-3 py-2 font-mono text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider">{s.asset_type}</td>
                  <td className="px-3 py-2 font-mono text-[var(--color-text)]">{s.detected_name}</td>
                  <td className="px-3 py-2 text-right text-[var(--color-text-faint)]">{s.call_count}</td>
                  <td className="px-3 py-2 text-right text-[var(--color-text-faint)]">{Math.round(s.confidence * 100)}%</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => onDraftOpen(s)}
                      className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                    >register →</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex items-center justify-center gap-3 pt-1">
        <button
          onClick={onRegisterOpen}
          className="px-3 py-1.5 rounded-md text-xs font-medium border border-[var(--color-border)] hover:border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] hover:bg-[var(--color-bg)] text-[var(--color-text)]"
        >+ Register asset manually</button>
      </div>
    </div>
  );
}

// ValidateSpecPanel — mirrors `nova validate <spec.yaml>`
function ValidateSpecPanel({ onFlash }: { onFlash: (tone: 'success' | 'error', text: string) => void }) {
  const [specYaml, setSpecYaml] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; valid: boolean; errors: string[]; note: string } | null>(null);

  const handleValidate = async () => {
    if (!specYaml.trim()) { onFlash('error', 'Paste a YAML spec first'); return; }
    setBusy(true);
    setResult(null);
    try {
      const res = await api.validateSpec(specYaml);
      setResult(res);
    } catch (e) {
      onFlash('error', `Validate request failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const inputClass = 'w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 font-mono text-xs focus:border-[var(--color-accent)] focus:outline-none';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Validate spec</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            CLI equivalent: <code className="font-mono">nova validate &lt;spec.yaml&gt;</code>
          </p>
        </div>
      </div>
      <label className="block">
        <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">YAML spec</span>
        <textarea
          value={specYaml}
          onChange={e => { setSpecYaml(e.target.value); setResult(null); }}
          placeholder={`name: my-agent\nversion: "1.0.0"\nasset_type: agent\ndescription: "My agent"\nowner: team@example.com`}
          rows={6}
          className={`mt-1 ${inputClass}`}
        />
      </label>
      <button
        onClick={handleValidate}
        disabled={busy || !specYaml.trim()}
        className="px-3 py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        {busy ? 'Validating…' : 'Validate'}
      </button>
      {result && (
        <div className={clsx(
          'rounded border px-3 py-2 text-xs space-y-1',
          result.valid
            ? 'border-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
            : 'border-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
        )}>
          <p className={clsx('font-medium', result.valid ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {result.valid ? 'Valid spec' : 'Invalid spec'}
          </p>
          {result.note && <p className="text-[var(--color-text-muted)]">{result.note}</p>}
          {result.errors.length > 0 && (
            <ul className="list-disc list-inside space-y-0.5 text-[var(--color-status-failure)] text-[11px]">
              {result.errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// ── SuggestRegisterPanel — dedicated panel for `nova suggest-register` ────────

function SuggestRegisterPanel({
  suggestions,
  onRefresh,
}: {
  suggestions: RegistrationSuggestion[] | null;
  onRefresh: () => void;
}) {
  const [registering, setRegistering] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ name: string; ok: boolean } | null>(null);
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (flashTimerRef.current) clearTimeout(flashTimerRef.current); }, []);

  const doRegister = async (s: RegistrationSuggestion) => {
    const key = `${s.asset_type}:${s.detected_name}`;
    setRegistering(key);
    try {
      await api.registerFromSuggestion(s.draft_spec_yaml);
      setFlash({ name: s.detected_name, ok: true });
      onRefresh();
    } catch {
      setFlash({ name: s.detected_name, ok: false });
    } finally {
      setRegistering(null);
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      flashTimerRef.current = setTimeout(() => setFlash(null), 2500);
    }
  };

  const total = suggestions?.length ?? 0;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
            Suggest Register
          </p>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Assets detected in recent capsules not yet in the registry.
          </p>
        </div>
        <button
          onClick={onRefresh}
          className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] transition-colors"
        >
          ↺ refresh
        </button>
      </div>

      {flash && (
        <p className={clsx(
          'text-[10px] font-mono',
          flash.ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
        )}>
          {flash.ok ? `✓ registered ${flash.name}` : `✗ failed to register ${flash.name}`}
        </p>
      )}

      {suggestions === null ? (
        <p className="text-[10px] text-[var(--color-text-faint)] italic">Scanning capsules…</p>
      ) : total === 0 ? (
        <p className="text-[10px] text-[var(--color-status-success)]">✓ No unregistered assets detected.</p>
      ) : (
        <div className="space-y-1.5">
          {suggestions.map((s) => {
            const key = `${s.asset_type}:${s.detected_name}`;
            const busy = registering === key;
            return (
              <div key={key} className="flex items-center gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5">
                <div className="flex-1 min-w-0 space-y-px">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[9px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">{s.asset_type}</span>
                    <span className="text-xs font-mono text-[var(--color-text)] truncate">{s.detected_name}</span>
                    {s.detected_version && (
                      <span className="text-[10px] text-[var(--color-text-faint)]">{s.detected_version}</span>
                    )}
                  </div>
                  <div className="text-[9px] text-[var(--color-text-faint)]">
                    {s.call_count} calls · {Math.round(s.confidence * 100)}% confidence
                    {s.warnings?.length > 0 && (
                      <span className="text-[var(--color-status-pending)] ml-1">⚠ {s.warnings[0]}</span>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => void doRegister(s)}
                  disabled={busy}
                  className={clsx(
                    'text-[10px] font-mono px-2 py-1 rounded border shrink-0 transition-colors',
                    busy
                      ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                      : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
                  )}
                >
                  {busy ? '…' : 'Register'}
                </button>
              </div>
            );
          })}
        </div>
      )}

      <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
        CLI: <code>nova suggest-register</code> · <code>nova suggest-register --auto --min-confidence 0.8</code>
      </p>
    </div>
  );
}

// ReportPanel — mirrors `nova report`
function ReportPanel({ onFlash }: { onFlash: (tone: 'success' | 'error', text: string) => void }) {
  const [format, setFormat] = useState<'json' | 'markdown'>('json');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; format: string; content: string; note: string } | null>(null);

  const handleGenerate = async () => {
    setBusy(true);
    setResult(null);
    try {
      const res = await api.assetReport(format);
      setResult(res);
      if (!res.ok) onFlash('error', `Report error: ${res.note}`);
    } catch (e) {
      onFlash('error', `Report request failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Asset inventory report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            CLI equivalent: <code className="font-mono">nova report --format {format}</code>
          </p>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <label className="block">
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Format</span>
          <select
            value={format}
            onChange={e => { setFormat(e.target.value as 'json' | 'markdown'); setResult(null); }}
            className="mt-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
          >
            <option value="json">json</option>
            <option value="markdown">markdown</option>
          </select>
        </label>
        <button
          onClick={handleGenerate}
          disabled={busy}
          className="mt-5 px-3 py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {busy ? 'Generating…' : 'Generate report'}
        </button>
      </div>
      {result && (
        <div className="space-y-1">
          <p className={clsx('text-[10px] font-medium', result.ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {result.note}
          </p>
          {result.content && (
            <pre className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-[10px] font-mono text-[var(--color-text-muted)] overflow-auto max-h-64 whitespace-pre-wrap break-all">
              {result.content}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// Private sub-component — only used in the eval confirm dialog above.
function EvalHistoryChart({ history }: { history: EvalHistoryEntry[] }) {
  const ordered = [...history].reverse(); // API returns newest-first; reverse to oldest-left
  const barW = 24;
  const gap = 3;
  const maxH = 64;
  const svgW = Math.max(1, ordered.length * (barW + gap) - gap);
  const svgH = 94; // bar area (64) + suite label (10) + timestamp (12) + score label buffer (8)

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg width={svgW} height={svgH} aria-label="Eval history chart">
        {ordered.map((entry, i) => {
          const x = i * (barW + gap);
          const barH = entry.score !== null ? Math.max(4, entry.score * maxH) : maxH;
          const barY = maxH - barH;
          const fill = entry.passed
            ? 'var(--color-status-success)'
            : 'var(--color-status-failure)';
          const suiteTrunc = entry.suite_name.length > 8
            ? `${entry.suite_name.slice(0, 7)}…`
            : entry.suite_name;

          return (
            <g key={entry.eval_id}>
              <rect x={x} y={barY} width={barW} height={barH} fill={fill} rx={2} />
              {entry.score !== null && (
                <text
                  x={x + barW / 2}
                  y={barY - 2}
                  textAnchor="middle"
                  fontSize={8}
                  fill="var(--color-text-muted)"
                >
                  {entry.score.toFixed(1)}
                </text>
              )}
              <text
                x={x + barW / 2}
                y={maxH + 10}
                textAnchor="middle"
                fontSize={8}
                fill="var(--color-text-faint)"
              >
                {suiteTrunc}
              </text>
              <text
                x={x + barW / 2}
                y={maxH + 20}
                textAnchor="middle"
                fontSize={7}
                fill="var(--color-text-faint)"
              >
                {relativeTime(entry.run_at)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
