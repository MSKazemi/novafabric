/**
 * ExportCenter — E1 (ADR-0201): one destination for every export the
 * dashboard can produce.
 *
 * The registry-driven compliance exports are the primary tool here, reusing
 * the existing GenericExportPanel (server-catalog driven — no export logic or
 * kind list is duplicated). Export families that already live in their own
 * subject tabs (evidence bundles, lineage documents, analytical reports) are
 * surfaced as a directory of jump links, so this is a discoverability hub, not
 * a re-implementation: each link routes to the tab that owns that export.
 */
import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import type { Tab } from './Sidebar';
import GenericExportPanel from './compliance/GenericExportPanel';

const JUMP_TARGETS: Array<{ tab: Tab; label: string; blurb: string }> = [
  { tab: 'evidence', label: 'Evidence bundles', blurb: 'Signed Evidence Bundle ZIPs (build, verify, download).' },
  { tab: 'lineage', label: 'Lineage exports', blurb: 'OpenLineage / PROV-JSON lineage documents for a run.' },
  { tab: 'reports', label: 'Reports', blurb: 'Analytical reports as HTML / PDF / CSV / JSON.' },
  { tab: 'compliance', label: 'Compliance suite', blurb: 'RO-Crate, C2PA, RoPA, AIBOM, NIST-RMF, HIPAA-proof panels.' },
];

export default function ExportCenter({ onNavigate }: { onNavigate: (tab: Tab) => void }) {
  const [runIds, setRunIds] = useState<string[]>([]);

  useEffect(() => {
    api.listRuns({ limit: 200 })
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {/* suggestions are optional */});
  }, []);

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-[var(--color-text)]">Export Center</h2>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          One place to produce any export. Registry exports run here; subject-specific
          exports link to their tab. Every export mirrors a <code className="font-mono">nova</code> CLI command.
        </p>
      </div>

      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Registry exports</h3>
        <p className="text-[10px] text-[var(--color-text-faint)]">
          Server-driven catalog (<code className="font-mono">GET /api/compliance/export/kinds</code>) —
          new kinds appear automatically.
        </p>
        <GenericExportPanel runIds={runIds} />
      </section>

      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Other export surfaces</h3>
        <div className="grid gap-2 sm:grid-cols-2">
          {JUMP_TARGETS.map((t) => (
            <button
              key={t.tab}
              onClick={() => onNavigate(t.tab)}
              className="text-left rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 hover:border-[var(--color-accent)] transition-colors"
            >
              <div className="text-xs font-medium text-[var(--color-text)]">{t.label} →</div>
              <div className="text-[10px] text-[var(--color-text-muted)] mt-0.5">{t.blurb}</div>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
