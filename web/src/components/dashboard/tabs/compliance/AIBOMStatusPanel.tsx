import { useState, useEffect } from 'react';
import { api } from '../../../../lib/api';

// ---------- AIBOM status panel (nova aibom status — cap-008) ----------

export default function AIBOMStatusPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.aibomStatus>> | null>(null);

  function handleLoad() {
    setLoading(true);
    setError(null);
    api.aibomStatus()
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(() => { handleLoad(); }, []);

  const coverageColor = result
    ? result.coverage_status === 'complete' ? 'var(--color-status-success)'
    : result.coverage_status === 'no_capsules' ? 'var(--color-text-faint)'
    : 'var(--color-status-warning)'
    : undefined;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">AI-SBOM Coverage Status</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            CRA SBOM compliance coverage — mirrors <code className="font-mono">nova aibom status</code>
          </p>
        </div>
        <button
          type="button"
          onClick={handleLoad}
          disabled={loading}
          className="text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-40 transition-colors shrink-0"
        >
          {loading ? '…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div className="text-lg font-mono font-bold text-[var(--color-text)]">{result.total_capsules}</div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">total</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div className="text-lg font-mono font-bold" style={{ color: 'var(--color-status-success)' }}>
                {result.capsules_with_aibom}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">with AIBOM</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div
                className="text-lg font-mono font-bold"
                style={{ color: result.capsules_missing_aibom > 0 ? 'var(--color-status-warning)' : 'var(--color-text-faint)' }}
              >
                {result.capsules_missing_aibom}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">missing</div>
            </div>
          </div>

          {result.total_capsules > 0 && (
            <div className="space-y-1">
              <div className="h-2 rounded bg-[var(--color-bg-sunken)] overflow-hidden">
                <div
                  className="h-2 rounded transition-all"
                  style={{
                    width: `${(result.capsules_with_aibom / result.total_capsules) * 100}%`,
                    background: coverageColor,
                  }}
                />
              </div>
              <div className="flex justify-between text-[9px] text-[var(--color-text-faint)]">
                <span style={{ color: coverageColor }} className="font-semibold uppercase tracking-wider">
                  {result.coverage_status}
                </span>
                <span>{Math.round((result.capsules_with_aibom / result.total_capsules) * 100)}% covered</span>
              </div>
            </div>
          )}

          <div className="text-[10px] space-y-0.5 text-[var(--color-text-faint)]">
            <div><span className="font-medium">Regulation:</span> {result.regulation}</div>
            <div><span className="font-medium">CRA deadline:</span> <span className="font-mono text-[var(--color-status-failure)]">{result.cra_deadline}</span></div>
            <div><span className="font-medium">Format:</span> {result.spec_version}</div>
          </div>

          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova aibom status
          </p>
        </div>
      )}
    </section>
  );
}
