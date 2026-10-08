/**
 * Infra tab — thin shell over the per-panel modules in `./infra/`.
 * The cards/panels themselves were extracted verbatim (behavior frozen);
 * this file only owns the run-id fetch, the header/footer chrome, and the
 * render order.
 */
import { useEffect, useState } from 'react';
import { api } from '../../../lib/api';
import {
  BackupCard,
  Card,
  CollectorCard,
  COMPONENTS,
  DockerRunnerCard,
  LineageStoreProfilePanel,
  MaintenanceCard,
  MCPRiskReportPanel,
  MCPScanPanel,
  ObjectStoreCard,
  StorageOpsCard,
} from './infra';

export default function InfraTab() {
  const [runIds, setRunIds] = useState<string[]>([]);
  useEffect(() => {
    api.listRuns().then(r => setRunIds(r.runs.map(run => run.run_id))).catch(() => {});
  }, []);
  const shipped = COMPONENTS.filter(c => c.badge === 'shipped').length;
  const partial = COMPONENTS.filter(c => c.badge === 'partial').length + 3; // +3 for live cards (Collector, DockerRunner, ObjectStore)
  const cliOnly = 0;

  return (
    <div className="space-y-4 max-w-4xl">
      {/* Header */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-5 py-4">
        <h2 className="text-sm font-semibold text-[var(--color-text)] mb-1">Infrastructure & Cluster-Scale Components</h2>
        <p className="text-xs text-[var(--color-text-muted)] leading-relaxed mb-3">
          All cluster-scale phases (Phases 0–6) shipped in v0.12. This tab shows the status of each component, what is available in the dashboard, and what requires the CLI.
        </p>
        <div className="flex flex-wrap gap-3">
          <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
            <span className="w-2 h-2 rounded-full bg-[var(--color-status-success)]" />
            {shipped} full dashboard UI
          </span>
          <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
            <span className="w-2 h-2 rounded-full bg-[var(--color-accent)]" />
            {partial} partial UI
          </span>
          <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
            <span className="w-2 h-2 rounded-full bg-[var(--color-status-pending)]" />
            {cliOnly} CLI only
          </span>
        </div>
      </div>

      {/* Live cards first, then static cards */}
      <div className="grid grid-cols-1 gap-3">
        <CollectorCard />
        <BackupCard />
        <MaintenanceCard />
        <DockerRunnerCard />
        <ObjectStoreCard />
        <StorageOpsCard runIds={runIds} />
        <LineageStoreProfilePanel />
        {COMPONENTS.map((c) => <Card key={c.title} c={c} />)}
      </div>

      <MCPScanPanel />

      <MCPRiskReportPanel />

      {/* Footer note */}
      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-4 py-3 text-[11px] text-[var(--color-text-faint)] leading-relaxed">
        <strong className="text-[var(--color-text)]">CLI-only operations</strong> are covered in the{' '}
        <span className="font-mono">Commands</span> tab — use the command builder to construct and copy any CLI command to your terminal.
        The dashboard auto-refreshes every 5 seconds when auto-refresh is enabled.
      </div>
    </div>
  );
}
