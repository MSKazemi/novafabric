/**
 * Compliance hub — a sub-navigated shell over the compliance panel manifest.
 *
 * The 22 panels themselves live in `./compliance/` (one file each, plus the
 * shared GenericExportPanel); this file only owns the run-id fetch, the
 * ?sub= group navigation, and rendering the active group's panels.
 */
import { useEffect, useState, type ComponentType } from 'react';
import { api } from '../../../lib/api';
import { useUrlState } from '../../../lib/useUrlState';
import SegmentedControl from '../../ui/primitives/SegmentedControl';
import TabShell from './TabShell';
import {
  COMPLIANCE_GROUPS,
  COMPLIANCE_PANELS,
  type CompliancePanelDef,
} from './compliance';

const VALID_GROUPS = new Set<string>(COMPLIANCE_GROUPS.map((g) => g.value));

const SEGMENTS = COMPLIANCE_GROUPS.map((g) => ({
  value: g.value,
  label: g.label,
  meta: COMPLIANCE_PANELS.filter((p) => p.group === g.value).length,
}));

function PanelHost({ def, runIds }: { def: CompliancePanelDef; runIds: string[] }) {
  if (def.needsRunIds) {
    const C = def.component as ComponentType<{ runIds: string[] }>;
    return <C runIds={runIds} />;
  }
  const C = def.component as ComponentType<Record<string, never>>;
  return <C />;
}

export default function ComplianceTab({ runIds: initialRunIds }: { runIds?: string[] }) {
  const [fetchedRunIds, setFetchedRunIds] = useState<string[]>([]);
  useEffect(() => {
    api.listRuns().then(r => setFetchedRunIds(r.runs.map(run => run.run_id))).catch(() => {});
  }, []);
  const ids = [...new Set([...(initialRunIds ?? []), ...fetchedRunIds])];

  const [rawSub, setSub] = useUrlState('sub', 'frameworks');
  // Normalize unknown ?sub= values back to the default group.
  const sub = VALID_GROUPS.has(rawSub) ? rawSub : 'frameworks';
  const activePanels = COMPLIANCE_PANELS.filter((p) => p.group === sub);

  return (
    <TabShell
      title="Compliance"
      icon="compliance"
      subtitle="EU AI Act · NIS2 · GDPR · NIST AI RMF — evidence-grade exports from Run Capsules"
      actions={
        <div className="flex gap-1.5">
          {(['ADR-0054', 'ADR-0055', 'ADR-0057', 'ADR-0061', 'ADR-0066'] as const).map(label => (
            <span
              key={label}
              className="text-2xs font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      }
      subnav={
        <SegmentedControl
          aria-label="Compliance areas"
          segments={SEGMENTS}
          value={sub}
          onChange={setSub}
        />
      }
    >
      <div className="space-y-4">
        {activePanels.map((def) => (
          <PanelHost key={def.id} def={def} runIds={ids} />
        ))}
      </div>
    </TabShell>
  );
}
