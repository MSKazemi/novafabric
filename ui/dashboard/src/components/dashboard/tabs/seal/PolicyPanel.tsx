// Promotion policy display panel. Extracted verbatim from SealTab.tsx
// (dashboard-modernization split).
import type { SealPolicyResponse } from '../../../../lib/api';
import EmptyState from '../../../ui/EmptyState';
import { fmt } from './helpers';

export default function PolicyPanel({ policy, error }: { policy: SealPolicyResponse | null; error: string | null }) {
  const labelClass = 'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';
  const valueClass = 'text-xs font-mono text-[var(--color-text)]';

  // Unconfigured (200 with configured:false) or a genuine fetch error both
  // render the same "no policy yet" empty state.
  if (error || (policy && (policy.configured === false || !policy.predicate))) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)] mb-3">
          Promotion Policy
        </p>
        <EmptyState
          variant="inline"
          message="No promotion policy configured"
          cliCommand="nova policy sign --proposer-key proposer.pem --proposer-cert proposer.crt ..."
        />
      </div>
    );
  }

  if (!policy) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 text-xs text-[var(--color-text-faint)]">
        Loading policy…
      </div>
    );
  }

  // Non-null: the guard above returns early when predicate is missing.
  const pred = policy.predicate!;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          Promotion Policy
        </p>
        <span className="font-mono text-[10px] px-2 py-0.5 rounded bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)] text-[var(--color-accent)]">
          v{policy.version}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-4 text-xs">
        <div className="space-y-1">
          <p className={labelClass}>Proposer key IDs</p>
          {pred.proposer_key_ids && pred.proposer_key_ids.length > 0 ? (
            <ul className="space-y-0.5">
              {pred.proposer_key_ids.map((k) => (
                <li key={k} className={`${valueClass} truncate`} title={k}>{k}</li>
              ))}
            </ul>
          ) : (
            <p className={valueClass}>—</p>
          )}
        </div>
        <div className="space-y-1">
          <p className={labelClass}>Approver key IDs</p>
          {pred.approver_key_ids && pred.approver_key_ids.length > 0 ? (
            <ul className="space-y-0.5">
              {pred.approver_key_ids.map((k) => (
                <li key={k} className={`${valueClass} truncate`} title={k}>{k}</li>
              ))}
            </ul>
          ) : (
            <p className={valueClass}>—</p>
          )}
        </div>
      </div>

      <div className="flex items-center gap-6 text-[10px] font-mono text-[var(--color-text-faint)]">
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">bypass window</span>
          {pred.bypass_valid_duration_hours ?? '—'}h
        </span>
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">self-approval</span>
          {pred.self_approval ? 'allowed' : 'prohibited'}
        </span>
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">threshold</span>
          {pred.approval_threshold ?? 1}
        </span>
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">created</span>
          {fmt(policy.created_at)}
        </span>
      </div>
    </div>
  );
}
