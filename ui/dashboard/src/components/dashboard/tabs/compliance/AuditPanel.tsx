import { useState, useCallback } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

// ---------- Compliance Audit panel ----------

const AUDIT_PROFILES = [
  'nist-ai-rmf',
  'eu-ai-act-high-risk',
  'gdpr',
  'soc2-type2',
  'iso42001',
  'scientific-reproducibility',
];

export default function AuditPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [profile, setProfile] = useState('nist-ai-rmf');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setReport(null);
    try {
      const res = await api.auditReport(id, profile);
      setReport(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, profile]);

  const overallScore = typeof report?.overall_score === 'number' ? report.overall_score as number : null;
  const totalControls = typeof report?.total_controls === 'number' ? report.total_controls as number : null;
  const coveredControls = typeof report?.covered_controls === 'number' ? report.covered_controls as number : null;
  const partialControls = typeof report?.partial_controls === 'number' ? report.partial_controls as number : null;
  const missingControls = typeof report?.missing_controls === 'number' ? report.missing_controls as number : null;
  const coverages = Array.isArray(report?.coverages) ? report!.coverages as Array<Record<string, unknown>> : null;
  const cliCmd = `nova audit report ${runId || '<capsule_dir>'} --profile ${profile}`;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Compliance Audit</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            ADR-0061 · map capsule evidence to regulatory controls
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">ADR-0061</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={run}
          placeholder="capsule run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        >
          {AUDIT_PROFILES.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>

      <div className="flex gap-2 items-center">
        <div className="flex-1 font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] truncate">
          $ {cliCmd}
        </div>
        <CopyButton text={cliCmd} label="CLI" />
        <button
          onClick={run}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
        >
          {loading ? '…' : 'Run Audit'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {report && overallScore !== null && (
        <div className="space-y-3">
          {/* Score bar */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[10px] font-mono text-[var(--color-text-faint)]">
              <span>Overall coverage</span>
              <span className={clsx(
                'font-bold',
                overallScore >= 0.8 ? 'text-[var(--color-status-success)]'
                  : overallScore >= 0.5 ? 'text-[var(--color-status-pending)]'
                  : 'text-[var(--color-status-failure)]',
              )}>
                {(overallScore * 100).toFixed(0)}%
              </span>
            </div>
            <div className="h-2 rounded-full bg-[var(--color-bg-sunken)] overflow-hidden border border-[var(--color-border)]">
              <div
                className={clsx(
                  'h-full rounded-full transition-all',
                  overallScore >= 0.8 ? 'bg-[var(--color-status-success)]'
                    : overallScore >= 0.5 ? 'bg-[var(--color-status-pending)]'
                    : 'bg-[var(--color-status-failure)]',
                )}
                style={{ width: `${Math.round(overallScore * 100)}%` }}
              />
            </div>
            <div className="flex gap-3 text-[9px] font-mono text-[var(--color-text-faint)]">
              {coveredControls !== null && <span className="text-[var(--color-status-success)]">{coveredControls} covered</span>}
              {partialControls !== null && partialControls > 0 && <span className="text-[var(--color-status-pending)]">{partialControls} partial</span>}
              {missingControls !== null && missingControls > 0 && <span className="text-[var(--color-status-failure)]">{missingControls} missing</span>}
              {totalControls !== null && <span>{totalControls} total</span>}
            </div>
          </div>

          {/* Control checklist */}
          {coverages && coverages.length > 0 && (
            <div className="max-h-64 overflow-y-auto space-y-1 pr-1">
              {coverages.map((cov, i) => {
                const status = String(cov.status ?? '');
                const statusColor =
                  status === 'covered' ? 'text-[var(--color-status-success)]'
                  : status === 'partial' ? 'text-[var(--color-status-pending)]'
                  : 'text-[var(--color-status-failure)]';
                const icon = status === 'covered' ? '✓' : status === 'partial' ? '◑' : '✗';
                return (
                  <div
                    key={i}
                    className="flex items-start gap-2 text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5"
                  >
                    <span className={clsx('shrink-0 font-mono font-bold mt-px', statusColor)}>{icon}</span>
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[var(--color-text-faint)]">{String(cov.control_id)}</span>
                        <span className="text-[var(--color-text-muted)] truncate">{String(cov.control_title)}</span>
                      </div>
                      {Array.isArray(cov.evidence_missing) && (cov.evidence_missing as unknown[]).length > 0 && (
                        <p className="text-[var(--color-status-failure)] text-[9px]">
                          Missing: {(cov.evidence_missing as string[]).join(', ')}
                        </p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
