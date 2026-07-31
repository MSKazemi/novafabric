/**
 * Registry tab — orchestrating shell over the `./registry/` split.
 *
 * Paging, sparkline lazy-loading, and every lifecycle-action dialog's state
 * live in `./registry/` hooks; the table, panels, and dialogs are one file
 * each. This file owns the search/status filter, the suggestion-engine
 * fetches, and composition. Extracted verbatim from the former 1.6K-line
 * monolith — behavior frozen; same default export + props.
 */
import { useState, useEffect, useMemo, useCallback } from 'react';
import { api } from '../../../lib/api';
import type { AssetSummary, RegistrationSuggestion } from '../../../lib/api';
import type { AssetRecord } from '../../../lib/fixtures';
import RegistryBrowser from '../../registry/RegistryBrowser';
import { ErrorBox, Loading } from '../helpers';
import type { Tab } from '../Sidebar';
import { useAssetsPage } from './registry/useAssetsPage';
import { useSparklineHistory } from './registry/useSparklineHistory';
import { useRegistryActions } from './registry/useRegistryActions';
import { nextStatusFor } from './registry/lifecycle';
import AssetList from './registry/AssetList';
import { SuggestionsBanner, SuggestionsEmptyState, SuggestRegisterPanel } from './registry/SuggestionsPanels';
import { ValidateSpecPanel, ReportPanel } from './registry/SpecPanels';
import {
  RegisterDialog, EvalDialog, PromoteDialog, RollbackDialog, UnregisterDialog,
} from './registry/LifecycleDialogs';
import CompareVersionsDialog from './registry/CompareVersionsDialog';
import ApprovalsDialog from './registry/ApprovalsDialog';
import SuggestionDraftDialog from './registry/SuggestionDraftDialog';

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
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'development' | 'staging' | 'production' | 'archived'>('all');

  // Offset-paged asset list + per-page detail/eval-result loading.
  const {
    assets, detailMap, evalResults, error, totalAssets, hasMore, loadingMore,
    refresh, loadMore, selectedIds, setSelectedIds,
  } = useAssetsPage({ refreshTick, onCountChange });

  // Lazy sparkline history via IntersectionObserver.
  const { sparklineHistory, rowRefCallback, refetch: refetchSparkline } = useSparklineHistory();

  // C-2 — suggestion engine (empty-state list + register-modal quick-picks)
  const [suggestions, setSuggestions] = useState<RegistrationSuggestion[] | null>(null);
  const [suggestionDraftOpen, setSuggestionDraftOpen] = useState<RegistrationSuggestion | null>(null);
  const [suggestionRegisterBusy, setSuggestionRegisterBusy] = useState(false);
  // Quick-pick suggestions shown inside the Register dialog
  const [registerSuggestions, setRegisterSuggestions] = useState<RegistrationSuggestion[]>([]);

  // Fetch suggestions whenever assets list changes
  useEffect(() => {
    if (assets === null) return;
    api.getSuggestions(10)
      .then((r) => setSuggestions(r.suggestions))
      .catch(() => setSuggestions([]));
  }, [assets]);

  // Build a map of assetName → sorted versions (from all assets, not just visible).
  // Computed here (before early returns) so the action callbacks can reference it.
  const versionsByName = (assets ?? []).reduce<Record<string, string[]>>((acc, a) => {
    if (!acc[a.name]) acc[a.name] = [];
    if (!acc[a.name].includes(a.version)) acc[a.name].push(a.version);
    return acc;
  }, {});

  // Dialog state + confirm handlers for every lifecycle action.
  const actions = useRegistryActions({
    assets, versionsByName, onFlash, refresh, selectedIds, setSelectedIds, refetchSparkline,
  });

  // Fetch quick-pick suggestions each time the register modal opens
  const { registerOpen } = actions;
  useEffect(() => {
    if (!registerOpen) { setRegisterSuggestions([]); return; }
    api.getSuggestions(8)
      .then((r) => setRegisterSuggestions(r.suggestions))
      .catch(() => setRegisterSuggestions([]));
  }, [registerOpen]);

  const onSuggestionRegister = useCallback(async (yaml: string) => {
    setSuggestionRegisterBusy(true);
    try {
      const res = await api.registerAsset(yaml);
      onFlash('success', `Registered ${res.asset.name}@${res.asset.version}`);
      setSuggestionDraftOpen(null);
      await refresh();
    } catch (e) {
      onFlash('error', `Register failed: ${(e as Error).message}`);
    } finally { setSuggestionRegisterBusy(false); }
  }, [onFlash, refresh]);

  const visibleAssetsMemo = useMemo(() => {
    if (!assets) return [];
    return assets.filter(a => {
      if (statusFilter !== 'all' && a.status !== statusFilter) return false;
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return a.name.toLowerCase().includes(q) || a.asset_type.toLowerCase().includes(q) || a.version.toLowerCase().includes(q);
    });
  }, [assets, statusFilter, search]);

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

  const onPromoteOpen = (a: AssetSummary) => {
    actions.setPromoteTarget(a);
    actions.setPromoteTo(nextStatusFor(a.status));
    actions.setPromoteError(null);
  };

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--color-text-muted)]">
        <span>
          {visibleAssets.length}{visibleAssets.length !== assets.length ? ` / ${assets.length}` : ''} asset{visibleAssets.length === 1 ? '' : 's'} · {evalResults.length} eval result{evalResults.length === 1 ? '' : 's'}
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => actions.setRegisterOpen(true)}
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
          onRegisterOpen={() => actions.setRegisterOpen(true)}
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

      <AssetList
        visibleAssets={visibleAssets}
        loadedCount={assets.length}
        totalAssets={totalAssets}
        selectedIds={selectedIds}
        setSelectedIds={setSelectedIds}
        sparklineHistory={sparklineHistory}
        rowRefCallback={rowRefCallback}
        versionsByName={versionsByName}
        onEval={actions.setEvalTarget}
        onPromote={onPromoteOpen}
        onCompareOpen={actions.onCompareOpen}
        onRollback={(a) => { actions.setRollbackTarget(a); actions.setRollbackReason(''); }}
        onApprovalOpen={actions.onApprovalOpen}
        onUnregister={(a) => { actions.setUnregisterTarget(a); actions.setUnregisterForce(false); }}
        bulkBusy={actions.bulkBusy}
        onBulkPromote={actions.onBulkPromote}
        hasMore={hasMore}
        loadingMore={loadingMore}
        loadMore={loadMore}
      />

      {/* Dialogs */}
      <RegisterDialog
        open={actions.registerOpen}
        yaml={actions.registerYaml}
        setYaml={actions.setRegisterYaml}
        suggestions={registerSuggestions}
        busy={actions.registerBusy}
        onConfirm={actions.onRegisterConfirm}
        onCancel={() => actions.setRegisterOpen(false)}
      />

      <EvalDialog
        target={actions.evalTarget}
        history={actions.evalHistory}
        historyLoading={actions.evalHistoryLoading}
        busy={actions.evalBusy}
        onConfirm={actions.onEvalConfirm}
        onCancel={() => actions.setEvalTarget(null)}
      />

      {/* Compare versions dialog (DC-6) */}
      {actions.compareAssetName !== null && (
        <CompareVersionsDialog
          assetName={actions.compareAssetName}
          versions={versionsByName[actions.compareAssetName] ?? []}
          fromVersion={actions.compareFromVersion}
          setFromVersion={actions.setCompareFromVersion}
          toVersion={actions.compareToVersion}
          setToVersion={actions.setCompareToVersion}
          busy={actions.compareBusy}
          result={actions.compareResult}
          setResult={actions.setCompareResult}
          error={actions.compareError}
          setError={actions.setCompareError}
          onSubmit={actions.onCompareSubmit}
          onClose={() => { actions.setCompareAssetName(null); actions.setCompareResult(null); actions.setCompareError(null); }}
        />
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
        <SuggestionDraftDialog
          suggestion={suggestionDraftOpen}
          busy={suggestionRegisterBusy}
          onRegister={(yaml) => void onSuggestionRegister(yaml)}
          onClose={() => setSuggestionDraftOpen(null)}
        />
      )}

      {/* DD-3: Rollback dialog */}
      <RollbackDialog
        target={actions.rollbackTarget}
        reason={actions.rollbackReason}
        setReason={actions.setRollbackReason}
        busy={actions.rollbackBusy}
        onConfirm={actions.onRollbackConfirm}
        onCancel={() => { actions.setRollbackTarget(null); actions.setRollbackReason(''); }}
      />

      {/* v0.20.0: Unregister dialog */}
      <UnregisterDialog
        target={actions.unregisterTarget}
        force={actions.unregisterForce}
        setForce={actions.setUnregisterForce}
        busy={actions.unregisterBusy}
        onConfirm={actions.onUnregisterConfirm}
        onCancel={() => { actions.setUnregisterTarget(null); actions.setUnregisterForce(false); }}
      />

      {/* DD-4: Approvals modal */}
      {actions.approvalAsset !== null && actions.approvalsSupported && (
        <ApprovalsDialog
          asset={actions.approvalAsset}
          approvals={actions.approvals}
          required={actions.approvalsRequired}
          loading={actions.approvalsLoading}
          role={actions.approveRole}
          setRole={actions.setApproveRole}
          note={actions.approveNote}
          setNote={actions.setApproveNote}
          busy={actions.approveBusy}
          onSubmit={actions.onApproveSubmit}
          onClose={() => { actions.setApprovalAsset(null); actions.setApproveNote(''); }}
        />
      )}

      <PromoteDialog
        target={actions.promoteTarget}
        promoteTo={actions.promoteTo}
        setPromoteTo={actions.setPromoteTo}
        force={actions.promoteForce}
        setForce={actions.setPromoteForce}
        busy={actions.promoteBusy}
        error={actions.promoteError}
        setError={actions.setPromoteError}
        onConfirm={actions.onPromoteConfirm}
        onCancel={() => { actions.setPromoteTarget(null); actions.setPromoteForce(false); actions.setPromoteError(null); }}
      />
    </div>
  );
}
