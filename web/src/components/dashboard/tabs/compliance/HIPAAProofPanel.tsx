import { useState } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

// ---------- HIPAA Safe Harbor proof panel (nova export-hipaa-proof) ----------

export default function HIPAAProofPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportHipaaProof>> | null>(null);

  function handleGenerate() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportHipaaProof(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  const statusColor = (status: string) =>
    status === 'not_present' || status === 'redacted'
      ? 'var(--color-status-success)'
      : status === 'not_assessable'
      ? 'var(--color-status-warning)'
      : 'var(--color-status-failure)';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">HIPAA Safe Harbor Proof (nova export-hipaa-proof)</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            45 CFR §164.514(b)(2) — technical evidence record for all 18 Safe Harbor identifier categories
          </p>
        </div>
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
          onClick={handleGenerate}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Generating…' : 'Generate Proof'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result?.ok && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-[10px] font-semibold text-[var(--color-text)]">
              {result.identifier_count}/18 identifier categories assessed
            </span>
            <span className="text-[9px] text-[var(--color-text-faint)] font-mono">{result.assessed_at}</span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>
          <div className="bg-[var(--color-bg-sunken)] rounded px-3 py-2">
            <p className="text-[10px] font-mono break-all text-[var(--color-text-muted)]">{result.proof_digest}</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] border-collapse">
              <thead>
                <tr className="border-b border-[var(--color-border)]">
                  <th className="text-left py-1 pr-3 font-medium text-[var(--color-text-faint)] uppercase tracking-wider">Category</th>
                  <th className="text-left py-1 pr-3 font-medium text-[var(--color-text-faint)] uppercase tracking-wider">Status</th>
                  <th className="text-left py-1 font-medium text-[var(--color-text-faint)] uppercase tracking-wider">Evidence Source</th>
                </tr>
              </thead>
              <tbody>
                {result.categories.map(cat => (
                  <tr key={cat.id} className="border-b border-[var(--color-border)] border-opacity-50">
                    <td className="py-1 pr-3 text-[var(--color-text-muted)]">{cat.name}</td>
                    <td className="py-1 pr-3">
                      <span
                        className="px-1 py-0.5 rounded text-[9px] font-mono font-semibold uppercase tracking-wider"
                        style={{
                          color: statusColor(cat.status),
                          background: `color-mix(in oklab, ${statusColor(cat.status)} 12%, transparent)`,
                        }}
                      >
                        {cat.status}
                      </span>
                    </td>
                    <td className="py-1 text-[var(--color-text-faint)] font-mono">{cat.evidence_source ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] italic text-[var(--color-text-faint)]">{result.disclaimer}</p>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-hipaa-proof {runId || '&lt;capsule_dir&gt;'} --output hipaa-proof.json
          </p>
        </div>
      )}
    </section>
  );
}
