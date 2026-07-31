// EU AI Act Art.12 export panel (nova euaiact export, ADR-0076). Extracted
// verbatim from GovernanceTab.tsx (dashboard-modernization split).
import { useState } from 'react';
import { api } from '../../../../lib/api';
import CopyButton from '../../../ui/CopyButton';

export default function EuAiActExportPanel() {
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    records: Array<{ run_id: string; created_at: string; status: string; logging_event_type: string[] }>;
    count: number;
    retention_months: number;
    mode: string;
  } | null>(null);

  function handleExport() {
    setLoading(true);
    setError(null);
    setResult(null);
    api.euaiactExport(fromDate || undefined, toDate || undefined)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">EU AI Act Art.12 Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export transparency logging records — mirrors <code className="font-mono">nova euaiact export</code>
          </p>
        </div>
        <span className="text-2xs font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0076
        </span>
      </div>

      <div className="flex flex-wrap gap-3 items-end">
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[var(--color-text-faint)]">From (ISO 8601, optional)</label>
          <input
            type="text"
            placeholder="2026-01-01T00:00:00Z"
            value={fromDate}
            onChange={e => setFromDate(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[var(--color-text-faint)]">To (ISO 8601, optional)</label>
          <input
            type="text"
            placeholder="2026-12-31T23:59:59Z"
            value={toDate}
            onChange={e => setToDate(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? 'Exporting…' : 'Export'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              {result.count} record{result.count !== 1 ? 's' : ''} · mode: <span className="font-mono">{result.mode}</span>
            </span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>

          {result.records.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--color-border)]">
                    <th className="text-left py-1 pr-3 text-[var(--color-text-faint)] font-medium">run_id</th>
                    <th className="text-left py-1 pr-3 text-[var(--color-text-faint)] font-medium">created_at</th>
                    <th className="text-left py-1 pr-3 text-[var(--color-text-faint)] font-medium">status</th>
                    <th className="text-left py-1 text-[var(--color-text-faint)] font-medium">event types</th>
                  </tr>
                </thead>
                <tbody>
                  {result.records.map((rec, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)] border-opacity-50 hover:bg-[var(--color-bg-sunken)]">
                      <td className="py-1 pr-3 text-[var(--color-text)]">{rec.run_id.slice(0, 24)}</td>
                      <td className="py-1 pr-3 text-[var(--color-text-faint)]">{rec.created_at}</td>
                      <td className="py-1 pr-3 text-[var(--color-text)]">{rec.status}</td>
                      <td className="py-1 text-[var(--color-text-faint)]">{rec.logging_event_type.join(', ')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-[10px] text-[var(--color-text-faint)]">
            Retention floor: {result.retention_months} months
          </p>
        </div>
      )}
    </section>
  );
}
