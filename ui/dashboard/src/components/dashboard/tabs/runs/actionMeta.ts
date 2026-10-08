/**
 * ConfirmDialog copy (title / description / CLI equivalent / confirm label)
 * for each run action. Extracted verbatim from the former RunsTab monolith —
 * behavior frozen.
 */
import type { RunSummary } from '../../../../lib/api';
import type { RunAction } from './types';

export interface ActionMeta {
  title: string;
  description: string;
  cliEquivalent: string;
  confirmLabel: string;
}

export function buildActionMeta(actionTarget: { run: RunSummary; action: RunAction } | null): ActionMeta | null {
  if (!actionTarget) return null;
  return {
    title: actionTarget.action === 'export' ? 'Export signed evidence bundle'
         : actionTarget.action === 'replay' ? 'Run forensic replay'
         : actionTarget.action === 'dry-run' ? 'Dry-run policy check'
         : actionTarget.action === 'semantic' ? 'Semantic similarity analysis'
         : actionTarget.action === 'exact' ? 'Exact replay eligibility check'
         : actionTarget.action === 'delete' ? 'Delete capsule'
         : 'Re-scan & redact capsule',
    description: actionTarget.action === 'export'
      ? `Build a tamper-evident, ed25519-signed ZIP from this capsule. Output goes to ~/.novafabric/evidence/${actionTarget.run.run_id}.zip.`
      : actionTarget.action === 'replay'
      ? 'Forensic mode: read-only inspection of what the capsule recorded. No subprocess spawned, no network calls. Safe to run anywhere.'
      : actionTarget.action === 'dry-run'
      ? 'Check which tool calls in this capsule would be blocked by the current replay policy — without executing anything. Reports ALLOW / BLOCK per tool call.'
      : actionTarget.action === 'semantic'
      ? 'Compute pairwise text similarity across model call responses in this capsule. Reports a similarity score (0–100%). Read-only.'
      : actionTarget.action === 'exact'
      ? 'Check whether this capsule meets exact replay requirements: deterministic env.lock and seeded model calls. Read-only.'
      : actionTarget.action === 'delete'
      ? `This permanently deletes capsule ${actionTarget.run.run_id} and cannot be undone. Blocked if any active legal hold exists.`
      : 'Re-run the secret scanner against this capsule and overwrite redaction-proof.json. Existing unsafe_skips are preserved.',
    cliEquivalent: actionTarget.action === 'export'
      ? `nova export-evidence ${actionTarget.run.capsule_path} --output ~/.novafabric/evidence/${actionTarget.run.run_id}.zip`
      : actionTarget.action === 'replay'
      ? `nova replay ${actionTarget.run.capsule_path} --mode forensic`
      : actionTarget.action === 'dry-run'
      ? `nova replay ${actionTarget.run.capsule_path} --mode forensic --dry-run`
      : actionTarget.action === 'semantic'
      ? `nova replay ${actionTarget.run.capsule_path} --mode semantic`
      : actionTarget.action === 'exact'
      ? `nova replay ${actionTarget.run.capsule_path} --mode exact`
      : actionTarget.action === 'delete'
      ? `nova capsule delete ${actionTarget.run.run_id} --registry default`
      : `nova redact ${actionTarget.run.capsule_path}`,
    confirmLabel: actionTarget.action === 'export' ? 'Export bundle'
                : actionTarget.action === 'replay' ? 'Run forensic replay'
                : actionTarget.action === 'dry-run' ? 'Run dry-run check'
                : actionTarget.action === 'semantic' ? 'Run semantic analysis'
                : actionTarget.action === 'exact' ? 'Run exact eligibility check'
                : actionTarget.action === 'delete' ? 'Delete capsule'
                : 'Re-scan & write proof',
  };
}
