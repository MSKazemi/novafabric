/**
 * Dialog state + confirm handlers for every Registry lifecycle action:
 * register, eval (with history), promote, rollback, unregister, approvals,
 * bulk promote, and version compare.
 *
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 * Each dialog's open-target state lives next to the callback that consumes it
 * so the shell only wires props through.
 */
import { useState, useEffect, useCallback } from 'react';
import { api } from '../../../../lib/api';
import type { AssetSummary, AssetDiffResult } from '../../../../lib/api';
import type { EvalHistoryEntry } from '../../EvalSparkline';
import { nextStatusFor } from './lifecycle';

export type ApprovalRecord = { role: string; actor: string; note: string; approved_at: string };

export function useRegistryActions({
  assets,
  versionsByName,
  onFlash,
  refresh,
  selectedIds,
  setSelectedIds,
  refetchSparkline,
}: {
  assets: AssetSummary[] | null;
  versionsByName: Record<string, string[]>;
  onFlash: (tone: 'success' | 'error', text: string) => void;
  refresh: () => Promise<void>;
  selectedIds: Set<string>;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  refetchSparkline: (assetId: string) => void;
}) {
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
  const [bulkBusy, setBulkBusy] = useState(false);

  // Eval dialog history (20 results for the selected asset).
  const [evalHistory, setEvalHistory] = useState<EvalHistoryEntry[] | null>(null);
  const [evalHistoryLoading, setEvalHistoryLoading] = useState(false);

  // DD-3: Rollback
  const [rollbackTarget, setRollbackTarget] = useState<AssetSummary | null>(null);
  const [rollbackReason, setRollbackReason] = useState('');
  const [rollbackBusy, setRollbackBusy] = useState(false);

  // DD-4: Approvals
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
      refetchSparkline(targetId);
      await refresh();
    } catch (e) {
      onFlash('error', `Eval failed: ${(e as Error).message}`);
    } finally { setEvalBusy(false); }
  }, [evalTarget, onFlash, refresh, refetchSparkline]);

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
  }, [selectedIds, assets, onFlash, refresh, setSelectedIds]);

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

  return {
    // register
    registerOpen, setRegisterOpen, registerYaml, setRegisterYaml, registerBusy, onRegisterConfirm,
    // eval
    evalTarget, setEvalTarget, evalBusy, evalHistory, evalHistoryLoading, onEvalConfirm,
    // promote
    promoteTarget, setPromoteTarget, promoteTo, setPromoteTo,
    promoteForce, setPromoteForce, promoteBusy, promoteError, setPromoteError, onPromoteConfirm,
    // rollback
    rollbackTarget, setRollbackTarget, rollbackReason, setRollbackReason, rollbackBusy, onRollbackConfirm,
    // unregister
    unregisterTarget, setUnregisterTarget, unregisterForce, setUnregisterForce, unregisterBusy, onUnregisterConfirm,
    // approvals
    approvalAsset, setApprovalAsset, approvals, approvalsRequired, approvalsSupported,
    approvalsLoading, approveRole, setApproveRole, approveNote, setApproveNote, approveBusy,
    onApprovalOpen, onApproveSubmit,
    // bulk
    bulkBusy, onBulkPromote,
    // compare
    compareAssetName, setCompareAssetName, compareFromVersion, setCompareFromVersion,
    compareToVersion, setCompareToVersion, compareBusy, compareResult, setCompareResult,
    compareError, setCompareError, onCompareOpen, onCompareSubmit,
  };
}
