/**
 * Audit Coverage panel (`nova audit coverage`) — exemplar of the
 * PanelScaffold + useMutation pattern. Same API call and rendered result data
 * as the pre-split bespoke panel; only the chrome comes from the scaffold.
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { useMutation } from '../../../../lib/useMutation';
import PanelScaffold from '../../PanelScaffold';
import { AUDIT_PROFILE_IDS, DEFAULT_AUDIT_PROFILE } from './auditProfiles';

type CoverageEntry = {
  control_id: string;
  control_name?: string;
  status: string;
  capsule_count?: number;
  evidence_count?: number;
  score?: number;
};

type AuditFinding = {
  /** `seal_invalid` blocks the gate; `seal_unbound` is a notice. A code this build does not know is shown as blocking. */
  code: 'seal_invalid' | 'seal_unbound' | (string & {});
  capsule_id: string;
  message: string;
};

/**
 * Findings that never gate (mirrors the server's non-blocking codes). `seal_unbound`: the
 * `.seal` verifies but does not name the capsule's evidence streams, so it could not show an
 * edit to them; it does not say the capsule was edited. An unknown code is treated as blocking.
 */
const NON_BLOCKING_FINDING_CODES: ReadonlySet<string> = new Set(['seal_unbound']);
const isBlockingFinding = (f: AuditFinding): boolean => !NON_BLOCKING_FINDING_CODES.has(f.code);

type CoverageResult = {
  profile: string;
  overall_score: number;
  threshold: number;
  /** score >= threshold only; it says nothing about findings. */
  threshold_met: boolean;
  /** score >= threshold AND no finding. Absent on a server older than the field. */
  gate_passed?: boolean;
  findings?: AuditFinding[];
  capsule_count: number;
  total_controls: number;
  covered_controls: number;
  partial_controls: number;
  missing_controls: number;
  coverages: CoverageEntry[];
};


export default function AuditCoveragePanel() {
  const [profile, setProfile] = useState<string>(DEFAULT_AUDIT_PROFILE);
  const [threshold, setThreshold] = useState(0.8);

  const coverage = useMutation(
    async (): Promise<CoverageResult> => {
      const r = await api.auditCoverage(profile, threshold);
      return { ...r, coverages: r.coverages as CoverageEntry[] };
    },
    { silentSuccess: true, silentError: true },
  );
  const result = coverage.result;
  // The verdict is the gate (score and no finding); `threshold_met` is only the fallback
  // for a server that does not send `gate_passed`.
  const gatePassed = result ? (result.gate_passed ?? result.threshold_met) : false;
  const findings = (result?.findings ?? []).filter(isBlockingFinding);
  const notices = (result?.findings ?? []).filter(f => !isBlockingFinding(f));

  return (
    <PanelScaffold
      id="audit-coverage"
      title="Audit Coverage"
      subtitle={
        <>
          Measure how many controls your run capsules cover — <code className="font-mono">nova audit coverage</code>
        </>
      }
      cli={`nova audit coverage --profile ${profile} --threshold ${threshold}`}
      form={
        <div className="flex flex-wrap gap-2 items-end">
          <label className="block space-y-1">
            <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Profile</span>
            <select
              value={profile}
              onChange={e => setProfile(e.target.value)}
              className="block text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
            >
              {AUDIT_PROFILE_IDS.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
          <label className="block space-y-1">
            <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Threshold</span>
            <input
              type="number"
              min={0} max={1} step={0.05}
              value={threshold}
              onChange={e => setThreshold(parseFloat(e.target.value))}
              className="block text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono w-20"
            />
          </label>
        </div>
      }
      onSubmit={() => void coverage.run()}
      submitLabel="Check coverage"
      pending={coverage.pending}
      error={coverage.error}
    >
      {result && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-4 text-[10px] font-mono">
            <span>
              <span className="text-[var(--color-text-faint)]">score </span>
              <span className={clsx('font-bold', gatePassed ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
                {(result.overall_score * 100).toFixed(1)}%
              </span>
              <span className="text-[var(--color-text-faint)]"> (threshold {(result.threshold * 100).toFixed(0)}%)</span>
            </span>
            <span className="text-[var(--color-text-faint)]">{result.total_controls} controls · {result.covered_controls} covered · {result.partial_controls} partial · {result.missing_controls} missing</span>
            <span className="text-[var(--color-text-faint)]">{result.capsule_count} capsules analysed</span>
            <span className={clsx('font-bold', gatePassed ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
              {gatePassed ? 'gate passed' : 'gate failed'}
            </span>
          </div>
          {findings.length > 0 && (
            <ul className="space-y-1 text-[10px] font-mono" aria-label="Audit findings">
              {findings.map((f, i) => (
                <li key={`${f.code}-${f.capsule_id}-${i}`} className="text-[var(--color-status-failure)]">
                  <span className="font-bold">{f.code}</span> · {f.capsule_id} — {f.message}
                </li>
              ))}
            </ul>
          )}
          {notices.length > 0 && (
            <ul className="space-y-1 text-[10px] font-mono" aria-label="Audit notices">
              {notices.map((f, i) => (
                <li key={`${f.code}-${f.capsule_id}-${i}`} className="text-[var(--color-text-faint)]">
                  <span className="font-bold">notice</span> · {f.code} · {f.capsule_id} — {f.message}
                </li>
              ))}
            </ul>
          )}
          {result.coverages.length > 0 && (
            <div className="rounded border border-[var(--color-border)] overflow-hidden max-h-64 overflow-y-auto">
              <table className="w-full text-[10px] font-mono">
                <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)] sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Control</th>
                    <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Status</th>
                    <th className="text-right px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Capsules</th>
                    <th className="text-right px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Score</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--color-border)]">
                  {result.coverages.map((c) => (
                    <tr key={c.control_id} className="hover:bg-[var(--color-bg-sunken)] transition-colors">
                      <td className="px-3 py-1.5 text-[var(--color-text)]">{c.control_id}{c.control_name ? ` — ${c.control_name}` : ''}</td>
                      <td className="px-3 py-1.5">
                        <span className={clsx(
                          'text-[9px] uppercase tracking-wider px-1 py-px rounded',
                          c.status === 'covered' ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                          : c.status === 'partial' ? 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]'
                          : 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]',
                        )}>{c.status}</span>
                      </td>
                      <td className="px-3 py-1.5 text-right text-[var(--color-text-faint)]">{c.capsule_count ?? '—'}</td>
                      <td className="px-3 py-1.5 text-right text-[var(--color-text-faint)]">
                        {c.score !== undefined ? `${(c.score * 100).toFixed(0)}%` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </PanelScaffold>
  );
}
