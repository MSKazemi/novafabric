/**
 * Governance tab — thin shell over the per-panel modules in `./governance/`.
 * The panels were extracted verbatim (behavior frozen); this file only owns
 * the run-id fetch, the header, the About card, and the render order.
 */
import { useEffect, useState } from 'react';
import { api } from '../../../lib/api';
import {
  ClassifyPanel,
  EuAiActExportPanel,
  EuAiActStatusPanel,
  EvalComparePanel,
  ManualClassifyPanel,
  VocabulariesPanel,
} from './governance';

export default function GovernanceTab() {
  const [runIds, setRunIds] = useState<string[]>([]);

  useEffect(() => {
    api.listRuns()
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-[var(--color-text)]">Governance</h2>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            AI system risk-tier classification — EU AI Act · NIST AI RMF · OMB M-24-10 · Eval regression comparison
          </p>
        </div>
        <div className="flex gap-1.5">
          {(['ADR-0056'] as const).map(label => (
            <span
              key={label}
              className="text-2xs font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      <ClassifyPanel runIds={runIds} />

      <VocabulariesPanel />

      <ManualClassifyPanel />

      <EvalComparePanel />

      <EuAiActStatusPanel />

      <EuAiActExportPanel />

      {/* About section */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-2">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          About risk classification
        </p>
        <p className="text-xs text-[var(--color-text-muted)]">
          NovaFabric classifies AI systems against three regulatory frameworks using a deterministic,
          rules-based pipeline (ADR-0056):
        </p>
        <ul className="text-xs text-[var(--color-text-muted)] space-y-1 pl-3 list-disc">
          <li>
            <span className="font-semibold text-[var(--color-text)]">EU AI Act (Regulation EU 2024/1689)</span>
            {' '}— four tiers: Prohibited, High-Risk (Annex III), Limited, Minimal.
          </li>
          <li>
            <span className="font-semibold text-[var(--color-text)]">NIST AI RMF 600-1</span>
            {' '}— four impact levels: Low, Medium, High, Critical.
          </li>
          <li>
            <span className="font-semibold text-[var(--color-text)]">OMB M-24-10</span>
            {' '}— rights-impacting and safety-impacting flags for US federal agencies.
          </li>
        </ul>
        <p className="text-[10px] text-[var(--color-text-faint)]">
          The dashboard panel classifies using metadata inferred from the capsule. For precise results,
          use <code className="font-mono">nova classify from-capsule</code> with a full system description.
        </p>
      </div>
    </div>
  );
}
