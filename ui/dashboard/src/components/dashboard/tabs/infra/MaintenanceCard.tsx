// P8-P10: Maintenance (safe, idempotent mutations). Extracted verbatim from
// InfraTab.tsx (dashboard-modernization split).
import { api } from '../../../../lib/api';
import { useMutation } from '../../../../lib/useMutation';
import ActionButton from '../../../ui/ActionButton';
import { BADGE_COLOR } from './badges';

export default function MaintenanceCard() {
  // Each action is idempotent and lossless: reindex rebuilds a cache that is
  // always regenerable from capsules; topology re-seed merges (duplicate node
  // IDs are no-ops); KG rebuild re-ingests. All confirm-gated per the
  // dashboard safe-mutations policy — none deletes user data.
  const reindex = useMutation(() => api.reindexRuns(), {
    successMessage: (r) => `Reindexed ${r.reindexed ?? 0} run(s) — ${r.total ?? 0} total`,
  });
  const reseed = useMutation(() => api.topologySeed(), {
    successMessage: (r) => `Topology seeded — +${r.agents_added ?? 0} agents, +${r.edges_added ?? 0} edges`,
  });
  const kgRebuild = useMutation(() => api.kgIngestAll(), {
    successMessage: (r) => (r.ok ? 'Knowledge graph re-ingested' : `KG error: ${r.error ?? 'unknown'}`),
  });

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-sm font-semibold text-[var(--color-text)]">Maintenance</h3>
          <span className={`text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
            safe · idempotent
          </span>
        </div>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">
          Confirmation-gated recompute actions — none deletes data
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <ActionButton
          onClick={() => reindex.run()}
          pending={reindex.pending}
          confirm={{ title: 'Reindex runs cache?', body: 'Rebuilds the runs index from every capsule on disk (INSERT-OR-REPLACE). Lossless and idempotent.', confirmLabel: 'Reindex' }}
        >
          Reindex runs cache
        </ActionButton>
        <ActionButton
          onClick={() => reseed.run()}
          pending={reseed.pending}
          confirm={{ title: 'Re-seed topology?', body: 'Merges capsules on disk into the live topology store. Duplicate nodes are no-ops (idempotent). Requires --topology.', confirmLabel: 'Re-seed' }}
        >
          Re-seed topology
        </ActionButton>
        <ActionButton
          onClick={() => kgRebuild.run()}
          pending={kgRebuild.pending}
          confirm={{ title: 'Rebuild knowledge graph?', body: 'Re-ingests all capsules into the KG (nova kg ingest --all). Idempotent merge.', confirmLabel: 'Rebuild' }}
        >
          Rebuild knowledge graph
        </ActionButton>
      </div>
      {(reindex.result?.error || reseed.result?.errors?.length) && (
        <p className="text-[10px] font-mono text-[var(--color-status-failure)] break-all">
          {reindex.result?.error ?? reseed.result?.errors?.join('; ')}
        </p>
      )}
    </div>
  );
}
