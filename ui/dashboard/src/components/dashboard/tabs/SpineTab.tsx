/**
 * Accountability Spine — surfaces three read-only, run-level evidence endpoints:
 *   GET /api/runs/{id}/energy       → energy receipts + conservation check (ADR-0093)
 *   GET /api/runs/{id}/ledger       → append-only log verify verdict (ADR-0094)
 *   GET /api/runs/{id}/safety-case  → structured safety case w/ claim backing (ADR-0095)
 *
 * Honesty rule: declared/unavailable energy is shown distinctly from measured;
 * UNSUPPORTED / CONTESTED claims are never rendered as compliant.
 */
import type { Tab } from '../Sidebar';
import { api } from '../../../lib/api';
import type { EnergyConfidence, SafetyBackingState } from '../../../lib/api';
import { useMutation } from '../../../lib/useMutation';
import { useUrlState } from '../../../lib/useUrlState';
import TabShell from './TabShell';
import ActionButton from '../../ui/ActionButton';

function ResultCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-xs space-y-1">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{title}</div>
      {children}
    </div>
  );
}

function conservationTone(status: string): string {
  const s = status.toLowerCase();
  if (s.includes('balanc')) return 'var(--color-status-success)';
  if (s.includes('diverg') || s.includes('fail') || s.includes('violat')) return 'var(--color-status-failure)';
  return 'var(--color-text-muted)';
}

function ledgerTone(status: string): string {
  const s = status.toUpperCase();
  if (s === 'OK') return 'var(--color-status-success)';
  if (s === 'TAMPER' || s === 'TRUNCATION' || s === 'REORDER') return 'var(--color-status-failure)';
  return 'var(--color-text-muted)';
}

const CONFIDENCE_TONE: Record<EnergyConfidence, string> = {
  measured: 'var(--color-status-success)',
  apportioned: 'var(--color-text-muted)',
  declared: 'var(--color-text-faint)',
  unknown: 'var(--color-text-faint)',
};

const BACKING_TONE: Record<SafetyBackingState, string> = {
  SUPPORTED: 'var(--color-status-success)',
  CONTESTED: 'var(--color-status-warning)',
  UNSUPPORTED: 'var(--color-status-failure)',
  UNKNOWN: 'var(--color-text-faint)',
};

function StatusChip({ status, tone }: { status: string; tone: string }) {
  return <span className="font-mono text-[10px] uppercase" style={{ color: tone }}>{status}</span>;
}

function fmtJoules(j: number | null): string {
  if (j === null || j === undefined) return '—';
  return j.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export default function SpineTab({ onNavigate }: { onNavigate?: (t: Tab) => void }) {
  const [runId, setRunId] = useUrlState('run_id', '');

  const energy = useMutation((rid: string) => api.getRunEnergy(rid), { silentSuccess: true });
  const ledger = useMutation((rid: string) => api.getRunLedger(rid), { silentSuccess: true });
  const safety = useMutation((rid: string) => api.getRunSafetyCase(rid), { silentSuccess: true });

  const rid = runId.trim();
  const disabled = !rid;

  const loadEvidence = () => {
    if (!rid) return;
    energy.run(rid);
    ledger.run(rid);
  };

  const claims = (safety.result?.nodes ?? []).filter((n) => n.node_type === 'claim');

  return (
    <TabShell
      title="Accountability Spine"
      subtitle="ADR-0093 / 0094 / 0095 — energy receipts, append-only ledger integrity, and the structured safety case for a run."
      cli={['GET /api/runs/{id}/energy', 'GET /api/runs/{id}/ledger', 'GET /api/runs/{id}/safety-case']}
      help="Enter a run ID and load its evidence: an energy conservation check (measured vs. declared joules), the append-only ledger verify verdict, and an on-demand structured safety case. Declared/unavailable energy and unsupported claims are surfaced honestly — never rendered as compliant."
    >
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
          Run ID
          <input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            placeholder="01JABC…"
            className="w-64 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
          />
        </label>
        <ActionButton onClick={loadEvidence} pending={energy.pending || ledger.pending} disabled={disabled} variant="primary">Load evidence</ActionButton>
        <ActionButton onClick={() => safety.run(rid)} pending={safety.pending} disabled={disabled}>Build safety-case</ActionButton>
        {onNavigate && (
          <ActionButton onClick={() => onNavigate('runs')} variant="ghost" size="sm">Browse runs →</ActionButton>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* Energy */}
        {energy.result && (
          <ResultCard title="Energy & carbon (ADR-0093)">
            {energy.result.conservation ? (
              <div className="space-y-0.5 mb-1">
                <div className="flex items-center gap-3">
                  <StatusChip status={energy.result.conservation.status} tone={conservationTone(energy.result.conservation.status)} />
                  {energy.result.conservation.divergence_pct !== null && (
                    <span className="text-[10px] text-[var(--color-text-faint)]">
                      divergence {energy.result.conservation.divergence_pct.toFixed(2)}%
                    </span>
                  )}
                </div>
                <div className="text-[10px] text-[var(--color-text-faint)] font-mono">
                  attributed {fmtJoules(energy.result.conservation.attributed_joules_sum)} J
                  {energy.result.conservation.node_measured_joules !== null && (
                    <> · node {fmtJoules(energy.result.conservation.node_measured_joules)} J</>
                  )}
                </div>
                <div className="text-[10px] text-[var(--color-text-faint)]">
                  {energy.result.conservation.receipts_measured} measured · {energy.result.conservation.receipts_declared} declared · {energy.result.conservation.receipts_unavailable} unavailable · {energy.result.conservation.receipts_total} total
                </div>
              </div>
            ) : (
              <p className="text-[var(--color-text-muted)]">unmeasurable — no conservation envelope</p>
            )}

            {energy.result.receipts.length === 0 ? (
              <p className="text-[var(--color-text-faint)]">no energy receipts</p>
            ) : (
              <table className="w-full text-[10px] font-mono">
                <thead>
                  <tr className="text-[var(--color-text-faint)] text-left">
                    <th className="font-normal pr-2">action</th>
                    <th className="font-normal pr-2">source</th>
                    <th className="font-normal pr-2">confidence</th>
                    <th className="font-normal text-right">joules</th>
                  </tr>
                </thead>
                <tbody>
                  {energy.result.receipts.slice(0, 20).map((r, i) => (
                    <tr key={`${r.action_ref.kind}-${r.action_ref.id}-${i}`} className="text-[var(--color-text-muted)]">
                      <td className="pr-2 truncate max-w-[10rem]">{r.action_ref.kind}</td>
                      <td className="pr-2 truncate max-w-[8rem]">{r.measurement_source}</td>
                      <td className="pr-2" style={{ color: CONFIDENCE_TONE[r.confidence] ?? 'var(--color-text-faint)' }}>{r.confidence}</td>
                      <td className="text-right tabular-nums">{fmtJoules(r.measured_joules)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </ResultCard>
        )}

        {/* Ledger */}
        {ledger.result && (
          <ResultCard title="Ledger integrity (ADR-0094)">
            <div className="flex items-center gap-3 mb-1">
              <StatusChip status={ledger.result.status} tone={ledgerTone(ledger.result.status)} />
              <span className="text-[10px] text-[var(--color-text-faint)]">exit {ledger.result.exit_code}</span>
            </div>
            {ledger.result.status.toUpperCase() === 'NO_CHECKPOINT' && (
              <p className="text-[var(--color-text-muted)]">not anchored — no checkpoint to verify against</p>
            )}
            {ledger.result.reasons.length > 0 && (
              <ul className="space-y-0.5">
                {ledger.result.reasons.map((reason, i) => (
                  <li key={i} className="font-mono text-[10px] text-[var(--color-text-muted)]">· {reason}</li>
                ))}
              </ul>
            )}
            {Object.keys(ledger.result.stream_verdicts).length > 0 && (
              <div className="pt-1 space-y-0.5">
                {Object.entries(ledger.result.stream_verdicts).map(([stream, verdict]) => (
                  <div key={stream} className="flex items-center gap-2 font-mono text-[10px]">
                    <StatusChip status={verdict} tone={ledgerTone(verdict)} />
                    <span className="truncate text-[var(--color-text-muted)]">{stream}</span>
                  </div>
                ))}
              </div>
            )}
          </ResultCard>
        )}

        {/* Safety-case */}
        {(safety.result || safety.error) && (
          <ResultCard title="Safety case (ADR-0095)">
            {safety.error ? (
              <p className="text-[var(--color-status-failure)]">{safety.error}</p>
            ) : safety.result ? (
              <>
                <div className="flex items-center gap-3 mb-1 text-[10px] text-[var(--color-text-faint)]">
                  <span className="font-mono">{safety.result.template_id}</span>
                  <span>{claims.length} claim{claims.length === 1 ? '' : 's'}</span>
                  <code className="truncate max-w-[8rem]">{safety.result.case_hash}</code>
                </div>
                {claims.length === 0 ? (
                  <p className="text-[var(--color-text-faint)]">no claims in case</p>
                ) : (
                  <div className="space-y-1">
                    {claims.map((c) => {
                      const state = (c.backing_state ?? 'UNKNOWN') as SafetyBackingState;
                      return (
                        <div key={c.id} className="space-y-0.5">
                          <div className="flex items-start gap-2">
                            <StatusChip status={state} tone={BACKING_TONE[state] ?? 'var(--color-text-faint)'} />
                            <span className="text-[10px] text-[var(--color-text-muted)]">{c.statement ?? c.id}</span>
                          </div>
                          {state === 'CONTESTED' && c.contest_reason && (
                            <p className="pl-6 text-[10px] text-[var(--color-status-warning)]">contested: {c.contest_reason}</p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </>
            ) : null}
          </ResultCard>
        )}
      </div>
    </TabShell>
  );
}
