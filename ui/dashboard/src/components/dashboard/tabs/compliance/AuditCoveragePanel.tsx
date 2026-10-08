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

type CoverageEntry = {
  control_id: string;
  control_name?: string;
  status: string;
  capsule_count?: number;
  evidence_count?: number;
  score?: number;
};

type CoverageResult = {
  profile: string;
  overall_score: number;
  threshold: number;
  threshold_met: boolean;
  capsule_count: number;
  total_controls: number;
  covered_controls: number;
  partial_controls: number;
  missing_controls: number;
  coverages: CoverageEntry[];
};

const PROFILES = ['nist-ai-rmf', 'eu-ai-act', 'iso-42001'];

export default function AuditCoveragePanel() {
  const [profile, setProfile] = useState('nist-ai-rmf');
  const [threshold, setThreshold] = useState(0.8);

  const coverage = useMutation(
    async (): Promise<CoverageResult> => {
      const r = await api.auditCoverage(profile, threshold);
      return { ...r, coverages: r.coverages as CoverageEntry[] };
    },
    { silentSuccess: true, silentError: true },
  );
  const result = coverage.result;

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
              {PROFILES.map(p => <option key={p} value={p}>{p}</option>)}
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
              <span className={clsx('font-bold', result.threshold_met ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
                {(result.overall_score * 100).toFixed(1)}%
              </span>
              <span className="text-[var(--color-text-faint)]"> (threshold {(result.threshold * 100).toFixed(0)}%)</span>
            </span>
            <span className="text-[var(--color-text-faint)]">{result.total_controls} controls · {result.covered_controls} covered · {result.partial_controls} partial · {result.missing_controls} missing</span>
            <span className="text-[var(--color-text-faint)]">{result.capsule_count} capsules analysed</span>
          </div>
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
