import { useState } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

// ---------- NIST AI RMF report panel (nova export-nist-rmf — cap-009) ----------

export default function NISTRMFExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportNistRmf>> | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportNistRmf(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  const riskColor = (level: string) =>
    level === 'low' ? 'var(--color-status-success)'
    : level === 'medium' ? 'var(--color-status-warning)'
    : 'var(--color-status-failure)';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">NIST AI RMF Report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Quantitative risk report (GOVERN/MAP/MEASURE/MANAGE) — mirrors <code className="font-mono">nova export-nist-rmf</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          cap-009
        </span>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Generating…' : 'Generate RMF Report'}
        </button>
      </div>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        NIST AI 100-1 (January 2023) — GOVERN · MAP · MEASURE · MANAGE
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <span
              className="text-sm font-mono font-bold"
              style={{ color: riskColor(result.risk_level) }}
            >
              {result.overall_score !== null ? result.overall_score.toFixed(2) : 'n/a'}
            </span>
            <span
              className="text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wider"
              style={{
                color: riskColor(result.risk_level),
                background: `color-mix(in oklab, ${riskColor(result.risk_level)} 12%, transparent)`,
              }}
            >
              {result.risk_level}
            </span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>
          <div className="space-y-1">
            {(['GOVERN', 'MAP', 'MEASURE', 'MANAGE'] as const).map(fn => {
              const fnMetrics = result.metrics.filter(m => m.function === fn);
              if (fnMetrics.length === 0) return null;
              const avg = fnMetrics.reduce((s, m) => s + (m.score ?? 0), 0) / fnMetrics.length;
              return (
                <div key={fn} className="flex items-center gap-2 text-[10px]">
                  <span className="font-mono font-semibold w-16 text-[var(--color-text)]">{fn}</span>
                  <div className="flex-1 h-1.5 rounded bg-[var(--color-bg-sunken)]">
                    <div
                      className="h-1.5 rounded transition-all"
                      style={{
                        width: `${avg * 100}%`,
                        background: riskColor(avg >= 0.7 ? 'low' : avg >= 0.4 ? 'medium' : 'high'),
                      }}
                    />
                  </div>
                  <span className="w-8 text-right text-[var(--color-text-muted)]">{(avg * 100).toFixed(0)}%</span>
                </div>
              );
            })}
          </div>
          {result.missing_evidence.length > 0 && (
            <div className="text-[10px] text-[var(--color-text-faint)]">
              Missing evidence: {result.missing_evidence.join(', ')}
            </div>
          )}
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-nist-rmf {runId} --output nist-rmf.json
          </p>
        </div>
      )}
    </section>
  );
}
