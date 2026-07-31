/**
 * Runs/Registry decomposition smoke tests: every extracted module imports
 * cleanly and exports what the shells expect, plus cheap jsdom renders of the
 * two pure-props list components (RunList / AssetList).
 *
 * Both tabs' data layers call @/lib/api, so the module is stubbed.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', () => ({
  api: {},
  getConnection: () => ({ token: null, base: '' }),
  openManagedRunStream: () => ({ close: () => {} }),
  ServeApiError: class ServeApiError extends Error {},
}));

import RunsTab from '@/components/dashboard/tabs/RunsTab';
import RunFilters from '@/components/dashboard/tabs/runs/RunFilters';
import RunList from '@/components/dashboard/tabs/runs/RunList';
import RunInspector from '@/components/dashboard/tabs/runs/RunInspector';
import SecretScanPanel from '@/components/dashboard/tabs/runs/SecretScanPanel';
import { ForensicResultPane, SemanticResultPane, ExactResultPane } from '@/components/dashboard/tabs/runs/ReplayResultPanes';
import { CapsuleTreePanel, RunSpoolLineagePanel, ScanSecretsPanel } from '@/components/dashboard/tabs/runs/ParityPanels';
import { SeverityBadge, SEVERITY_STYLE } from '@/components/dashboard/tabs/runs/severity';
import { buildActionMeta } from '@/components/dashboard/tabs/runs/actionMeta';
import { useRunSearch } from '@/components/dashboard/tabs/runs/useRunSearch';
import { extractScenario } from '@/components/dashboard/tabs/runs/types';

import RegistryTab from '@/components/dashboard/tabs/RegistryTab';
import AssetList from '@/components/dashboard/tabs/registry/AssetList';
import { SuggestionsBanner, SuggestionsEmptyState, SuggestRegisterPanel } from '@/components/dashboard/tabs/registry/SuggestionsPanels';
import { ValidateSpecPanel, ReportPanel } from '@/components/dashboard/tabs/registry/SpecPanels';
import { RegisterDialog, EvalDialog, PromoteDialog, RollbackDialog, UnregisterDialog } from '@/components/dashboard/tabs/registry/LifecycleDialogs';
import CompareVersionsDialog from '@/components/dashboard/tabs/registry/CompareVersionsDialog';
import ApprovalsDialog from '@/components/dashboard/tabs/registry/ApprovalsDialog';
import SuggestionDraftDialog from '@/components/dashboard/tabs/registry/SuggestionDraftDialog';
import { nextStatusFor, validTargetsFor, StatusBadge } from '@/components/dashboard/tabs/registry/lifecycle';
import { useAssetsPage } from '@/components/dashboard/tabs/registry/useAssetsPage';
import { useSparklineHistory } from '@/components/dashboard/tabs/registry/useSparklineHistory';
import { useRegistryActions } from '@/components/dashboard/tabs/registry/useRegistryActions';
import type { AssetSummary, RunSummary } from '@/lib/api';

describe('runs split modules', () => {
  it('exports components and hooks as functions', () => {
    for (const c of [
      RunsTab, RunFilters, RunList, RunInspector, SecretScanPanel,
      ForensicResultPane, SemanticResultPane, ExactResultPane,
      CapsuleTreePanel, RunSpoolLineagePanel, ScanSecretsPanel,
      SeverityBadge, buildActionMeta, useRunSearch, extractScenario,
    ]) {
      expect(typeof c).toBe('function');
    }
    expect(SEVERITY_STYLE.critical).toBeTruthy();
  });

  it('buildActionMeta covers every action and returns null without a target', () => {
    expect(buildActionMeta(null)).toBeNull();
    const run = { run_id: 'r-1', capsule_path: '/tmp/r-1' } as unknown as RunSummary;
    for (const action of ['export', 'replay', 'dry-run', 'semantic', 'exact', 'redact', 'delete'] as const) {
      const meta = buildActionMeta({ run, action });
      expect(meta).not.toBeNull();
      expect(meta!.title.length).toBeGreaterThan(0);
      expect(meta!.cliEquivalent).toContain('nova ');
      expect(meta!.confirmLabel.length).toBeGreaterThan(0);
    }
  });

  it('extractScenario pulls the scenario directory segment', () => {
    expect(extractScenario(['run', '--scenario', 'scenarios/c1_break/scenario.yaml'])).toBe('c1_break');
    expect(extractScenario(['run'])).toBeNull();
  });

  it('RunList renders the empty state when nothing is loaded', () => {
    render(
      <RunList
        visibleRuns={[]}
        loadedCount={0}
        totalApprox={0}
        selected={null}
        onSelect={() => {}}
        checkedIds={[]}
        setCheckedIds={() => {}}
        compareA={null}
        setCompareA={() => {}}
        costMap={{}}
        validationStates={{}}
        onValidate={() => {}}
        onAction={() => {}}
        onShowSecrets={() => {}}
        hasMore={false}
        loadingMore={false}
        loadMore={() => {}}
      />,
    );
    expect(screen.getByText('No capsules yet.')).toBeTruthy();
  });

  it('RunList shows the truncation notice with a Load more affordance', () => {
    render(
      <RunList
        visibleRuns={[]}
        loadedCount={50}
        totalApprox={120}
        selected={null}
        onSelect={() => {}}
        checkedIds={[]}
        setCheckedIds={() => {}}
        compareA={null}
        setCompareA={() => {}}
        costMap={{}}
        validationStates={{}}
        onValidate={() => {}}
        onAction={() => {}}
        onShowSecrets={() => {}}
        hasMore={true}
        loadingMore={false}
        loadMore={() => {}}
      />,
    );
    expect(screen.getByText('Load more')).toBeTruthy();
  });
});

describe('registry split modules', () => {
  it('exports components and hooks as functions', () => {
    for (const c of [
      RegistryTab, AssetList, SuggestionsBanner, SuggestionsEmptyState,
      SuggestRegisterPanel, ValidateSpecPanel, ReportPanel,
      RegisterDialog, EvalDialog, PromoteDialog, RollbackDialog, UnregisterDialog,
      CompareVersionsDialog, ApprovalsDialog, SuggestionDraftDialog,
      StatusBadge, nextStatusFor, validTargetsFor,
      useAssetsPage, useSparklineHistory, useRegistryActions,
    ]) {
      expect(typeof c).toBe('function');
    }
  });

  it('lifecycle helpers stay frozen', () => {
    expect(nextStatusFor('development')).toBe('staging');
    expect(nextStatusFor('staging')).toBe('production');
    expect(nextStatusFor('production')).toBe('archived');
    expect(validTargetsFor('staging')).toEqual(['production', 'archived']);
    expect(validTargetsFor('archived')).toEqual(['staging']);
  });

  it('AssetList renders headers, truncation notice, and the bulk bar', () => {
    const asset = {
      id: 'a-1', name: 'my-agent', version: '1.0.0',
      asset_type: 'agent', status: 'development',
    } as unknown as AssetSummary;
    render(
      <AssetList
        visibleAssets={[asset]}
        loadedCount={50}
        totalAssets={80}
        selectedIds={new Set(['a-1'])}
        setSelectedIds={() => {}}
        sparklineHistory={{}}
        rowRefCallback={() => {}}
        versionsByName={{ 'my-agent': ['1.0.0'] }}
        onEval={() => {}}
        onPromote={() => {}}
        onCompareOpen={() => {}}
        onRollback={() => {}}
        onApprovalOpen={() => {}}
        onUnregister={() => {}}
        bulkBusy={false}
        onBulkPromote={() => {}}
        hasMore={true}
        loadingMore={false}
        loadMore={() => {}}
      />,
    );
    expect(screen.getByText('Asset')).toBeTruthy();
    expect(screen.getByText('Trend')).toBeTruthy();
    expect(screen.getByText('Load more')).toBeTruthy();
    expect(screen.getByText('Promote 1')).toBeTruthy();
  });
});
