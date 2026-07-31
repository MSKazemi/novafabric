/**
 * Tab decomposition split: every module extracted out of the five former
 * monolith tabs (Infra / Seal / Admin / KG / Governance) must export a real
 * component, and cheap jsdom smoke renders confirm the extracted panels still
 * mount. Fetch-on-mount panels get a stubbed @/lib/api (as in
 * compliance-hub.test.tsx).
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '@/lib/ToastContext';

import * as infra from '@/components/dashboard/tabs/infra';
import * as seal from '@/components/dashboard/tabs/seal';
import * as admin from '@/components/dashboard/tabs/admin';
import * as kg from '@/components/dashboard/tabs/kg';
import * as governance from '@/components/dashboard/tabs/governance';
import InfraTab from '@/components/dashboard/tabs/InfraTab';
import SealTab from '@/components/dashboard/tabs/SealTab';
import AdminTab from '@/components/dashboard/tabs/AdminTab';
import KGTab from '@/components/dashboard/tabs/KGTab';
import GovernanceTab from '@/components/dashboard/tabs/GovernanceTab';

vi.mock('@/lib/api', () => ({
  api: {
    // shells
    listRuns: async () => ({ runs: [] }),
    sealGetPolicy: async () => ({ configured: false }),
    listTokens: async () => ({ session_token_fingerprint: 'fp', tokens: [] }),
    listRoles: async () => ({ server_mode: false, roles: [], message: '' }),
    kgStatus: async () => ({ store: 'kuzu', store_health: 'ok', db_path: '/tmp/kg', edge_count: 0 }),
    // fetch-on-mount panels
    collectorStatus: async () => ({ detected: false }),
    backupStatus: async () => ({ detected: false, backups: [] }),
    storageStats: async () => ({ configured: false }),
    storageManifestChain: async () => ({ entries: [] }),
    adminApiKeys: async () => ({ keys: [] }),
    euaiactStatus: async () => ({
      ok: true, high_risk: false, provider_mode: false,
      retention_months: 6, deadline: '2026-08-02', note: '',
    }),
  },
  // useMutation imports ServeApiError from the same module.
  ServeApiError: class ServeApiError extends Error {},
}));

const EXTRACTED: Array<[string, Record<string, unknown>, string[]]> = [
  ['infra', infra, [
    'CmdBadge', 'StatRow', 'Card', 'CollectorCard', 'MaintenanceCard', 'BackupCard',
    'DockerRunnerCard', 'ObjectStoreCard', 'StorageOpsCard', 'LineageStoreProfilePanel',
    'MCPScanPanel', 'MCPRiskReportPanel',
  ]],
  ['seal', seal, [
    'PolicyPanel', 'ProposalsPanel', 'BypassSodPanel', 'CapsuleVerifyPanel',
    'SigstoreSignPanel', 'SigstoreVerifyPanel', 'MerkleLogVerifyPanel', 'RatchetPanel',
  ]],
  ['admin', admin, [
    'SectionHeading', 'Panel', 'ConfirmDialog', 'CliRefRow', 'IssueTokenDialog', 'TokenRow',
    'ApiKeysPanel', 'NewRunIdPanel', 'DoctorPanel', 'IngestCapsulePanel',
    'RoleManagementPanel', 'FlushJwksCachePanel', 'DatabaseOpsPanel',
  ]],
  ['kg', kg, [
    'StatusPanel', 'AgentQueryPanel', 'TopologyLayerPanel', 'KGInitPanel', 'KGIngestPanel',
    'KGIngestAllPanel', 'KGQueryPanel', 'KGAuditPanel', 'EntityQueuePanel', 'KGAliasPanel',
  ]],
  ['governance', governance, [
    'ClassifyPanel', 'ManualClassifyPanel', 'VocabulariesPanel', 'EvalComparePanel',
    'EuAiActStatusPanel', 'EuAiActExportPanel',
  ]],
];

describe('tab split modules', () => {
  for (const [dir, mod, names] of EXTRACTED) {
    describe(dir, () => {
      for (const name of names) {
        it(`exports ${name} as a component`, () => {
          expect(typeof mod[name]).toBe('function');
        });
      }
    });
  }

  it('infra keeps the static COMPONENTS manifest', () => {
    expect(Array.isArray(infra.COMPONENTS)).toBe(true);
    expect(infra.COMPONENTS.length).toBe(8);
  });
});

describe('tab shells still render (smoke)', () => {
  function renderWithToast(ui: React.ReactElement) {
    return render(<ToastProvider>{ui}</ToastProvider>);
  }

  it('InfraTab renders its header and panels', () => {
    renderWithToast(<InfraTab />);
    expect(screen.getByText('Infrastructure & Cluster-Scale Components')).toBeInTheDocument();
    expect(screen.getByText('Maintenance')).toBeInTheDocument();
    expect(screen.getByText('MCP Supply-Chain Risk Scanner')).toBeInTheDocument();
  });

  it('SealTab renders its header and panels', () => {
    renderWithToast(<SealTab />);
    expect(screen.getByText('NovaSeal Maker-Checker')).toBeInTheDocument();
    expect(screen.getByText('Capsule proposals')).toBeInTheDocument();
    expect(screen.getByText('Bypass SoD Requirement')).toBeInTheDocument();
    expect(screen.getByText('Merkle Log Verify')).toBeInTheDocument();
  });

  it('AdminTab renders its panels after load', async () => {
    renderWithToast(<AdminTab />);
    expect(await screen.findByText('Admin Panel')).toBeInTheDocument();
    expect(screen.getByText('Issued tokens')).toBeInTheDocument();
    expect(screen.getByText('Database operations')).toBeInTheDocument();
  });

  it('KGTab renders its header and panels', () => {
    renderWithToast(<KGTab />);
    expect(screen.getByText('Capsule Knowledge Graph')).toBeInTheDocument();
    expect(screen.getByText('Multi-Layer Topology')).toBeInTheDocument();
    expect(screen.getByText('KG Alias Management')).toBeInTheDocument();
  });

  it('GovernanceTab renders its header and panels', () => {
    renderWithToast(<GovernanceTab />);
    expect(screen.getByText('Governance')).toBeInTheDocument();
    expect(screen.getByText('Risk Classification')).toBeInTheDocument();
    expect(screen.getByText('Eval Regression Comparison')).toBeInTheDocument();
    expect(screen.getByText('EU AI Act Art.12 Export')).toBeInTheDocument();
  });
});
