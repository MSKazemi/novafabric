// EU AI Act Art.12 status panel (nova euaiact status, ADR-0076). Extracted
// verbatim from GovernanceTab.tsx (dashboard-modernization split).
import { useEffect, useState } from 'react';
import { api } from '../../../../lib/api';

export default function EuAiActStatusPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    high_risk: boolean;
    provider_mode: boolean;
    retention_months: number;
    deadline: string;
    note: string;
  } | null>(null);

  useEffect(() => {
    setLoading(true);
    api.euaiactStatus()
      .then(r => { setResult(r); setError(null); })
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">EU AI Act Art.12 Status</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Transparency logging status — mirrors <code className="font-mono">nova euaiact status</code>
          </p>
        </div>
        <span className="text-[var(--text-2xs)] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0076
        </span>
      </div>

      {loading && (
        <p className="text-xs text-[var(--color-text-faint)]">Loading…</p>
      )}

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
            <span className="text-[var(--color-text-faint)]">High-Risk Mode</span>
            <span>
              {result.high_risk
                ? <span className="text-[var(--color-status-success)] font-medium">active</span>
                : <span className="text-[var(--color-text-faint)]">inactive</span>}
            </span>

            <span className="text-[var(--color-text-faint)]">Role</span>
            <span className="text-[var(--color-text)] font-mono">
              {result.provider_mode ? 'provider' : 'deployer'}
            </span>

            <span className="text-[var(--color-text-faint)]">Retention floor</span>
            <span className="text-[var(--color-text)]">
              {result.retention_months} months (Art.18 provider / Art.12 deployer)
            </span>

            <span className="text-[var(--color-text-faint)]">Art.50 deadline</span>
            <span className="text-[var(--color-text)] font-mono">2026-08-02</span>
          </div>

          {result.note && (
            <p className="text-[10px] text-[var(--color-text-faint)] border-t border-[var(--color-border)] pt-2">
              {result.note}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
