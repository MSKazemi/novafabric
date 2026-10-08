/**
 * KG tab — thin shell over the per-panel modules in `./kg/`.
 * The panels were extracted verbatim (behavior frozen); this file only owns
 * the status + run-id fetches, the header, the About card, and the render
 * order. (DB-KG-1 — Capsule Knowledge Graph tab, v0.18.0; ADR-0067)
 */
import { useEffect, useState } from 'react';
import { api } from '../../../lib/api';
import {
  AgentQueryPanel,
  EntityQueuePanel,
  KGAliasPanel,
  KGAuditPanel,
  KGIngestAllPanel,
  KGIngestPanel,
  KGInitPanel,
  KGQueryPanel,
  StatusPanel,
  TopologyLayerPanel,
  type KGStatus,
} from './kg';

export default function KGTab() {
  const [status, setStatus] = useState<KGStatus | null>(null);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  const [runIds, setRunIds] = useState<string[]>([]);

  useEffect(() => {
    api.kgStatus()
      .then(setStatus)
      .catch((e) => setStatusErr((e as Error).message));
    api.listRuns()
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-[var(--color-text)]">Capsule Knowledge Graph</h2>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Auto-discovered topology: agents, models, MCP servers, tools, endpoints — CRDT-aggregated across capsules (ADR-0067)
          </p>
        </div>
        <div className="flex gap-1.5">
          {(['ADR-0067', 'v0.27.0'] as const).map((label) => (
            <span
              key={label}
              className="text-2xs font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      <StatusPanel status={status} error={statusErr} />
      <TopologyLayerPanel />
      <AgentQueryPanel runIds={runIds} />
      <KGInitPanel onDone={() => {
        api.kgStatus().then(setStatus).catch(() => {});
      }} />
      <KGIngestPanel capsuleDirs={runIds} />
      <KGIngestAllPanel onDone={() => {
        api.kgStatus().then(setStatus).catch(() => {});
      }} />
      <KGQueryPanel />
      <KGAuditPanel />
      <EntityQueuePanel />
      <KGAliasPanel />

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-2">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          About the Capsule KG
        </p>
        <p className="text-xs text-[var(--color-text-muted)]">
          The KG is a <span className="font-semibold">secondary derived artifact</span> built from
          observed capsule events. It is stored in a SEPARATE KuzuDB instance from lineage and
          accumulates CRDT counts so distributed collectors can merge without coordination.
        </p>
        <ul className="text-xs text-[var(--color-text-muted)] space-y-1 pl-3 list-disc">
          <li>Optional extra: <code className="font-mono">pip install 'novafabric[scale-kg]'</code> (kuzu&gt;=0.11.3)</li>
        </ul>
      </div>
    </div>
  );
}
