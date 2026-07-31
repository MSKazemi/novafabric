/**
 * Seal tab — thin shell over the per-panel modules in `./seal/`.
 * The panels were extracted verbatim (behavior frozen); this file only owns
 * the policy + run-id fetches, the header, and the render order.
 */
import { useEffect, useState } from 'react';
import { api, type SealPolicyResponse } from '../../../lib/api';
import {
  BypassSodPanel,
  CapsuleVerifyPanel,
  MerkleLogVerifyPanel,
  PolicyPanel,
  ProposalsPanel,
  RatchetPanel,
  RedactionXrayPanel,
  SigstoreSignPanel,
  SigstoreVerifyPanel,
  TrustRadarPanel,
} from './seal';

export default function SealTab() {
  const [policy, setPolicy] = useState<SealPolicyResponse | null>(null);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [runIds, setRunIds] = useState<string[]>([]);

  // Load policy on mount
  useEffect(() => {
    api.sealGetPolicy()
      .then(setPolicy)
      .catch(() => setPolicyError('not found'));
  }, []);

  // Load run IDs for SuggestInput
  useEffect(() => {
    api.listRuns()
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between text-xs">
        <div>
          <p className="text-sm font-semibold text-[var(--color-text)]">NovaSeal Maker-Checker</p>
          <p className="text-[var(--color-text-faint)] text-[10px] font-mono mt-0.5">ADR-0059 · nova seal propose / approve / verify</p>
        </div>
      </div>

      {/* Policy panel */}
      <PolicyPanel policy={policy} error={policyError} />

      {/* Capsule lookup + proposals */}
      <ProposalsPanel runIds={runIds} />

      {/* Bypass SoD Requirement */}
      <BypassSodPanel runIds={runIds} />

      {/* Capsule integrity verify */}
      <CapsuleVerifyPanel runIds={runIds} />

      {/* Trust surfaces — the visual half of ADR-0173 / ADR-0174 */}
      <TrustRadarPanel runIds={runIds} />
      <RedactionXrayPanel runIds={runIds} />

      {/* Sigstore keyless signing / verification (v0.44.0) */}
      <SigstoreSignPanel runIds={runIds} />
      <SigstoreVerifyPanel runIds={runIds} />

      {/* Merkle Log Verify */}
      <MerkleLogVerifyPanel runIds={runIds} />

      {/* Forward-secure signing key ratchet (ADR-0089) */}
      <RatchetPanel />
    </div>
  );
}
