import { useState } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

// ---------- AI-SBOM export panel (nova export-aibom — cap-008) ----------

export default function AIBOMExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportAibom>> | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportAibom(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">AI-SBOM Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export CycloneDX 1.6 ML-BOM — mirrors <code className="font-mono">nova export-aibom</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          cap-008
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
          {loading ? 'Exporting…' : 'Export AI-SBOM'}
        </button>
      </div>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        CycloneDX ML-BOM 1.6 (ECMA-424 2nd Edition) · EU CRA deadline 2026-09-11
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              {result.component_count} component{result.component_count !== 1 ? 's' : ''} · {result.bom_format}
            </span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>
          <div className="space-y-1">
            {result.components.map((comp, i) => (
              <div key={i} className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded px-2 py-1 flex items-center gap-2">
                <span className="text-[var(--color-text-faint)] uppercase tracking-wider text-[8px]">{comp.type}</span>
                <span className="font-semibold">{comp.name}</span>
                {comp.version && <span className="text-[var(--color-text-muted)]">@{comp.version}</span>}
                {comp.description && <span className="text-[var(--color-text-faint)] truncate max-w-xs">{comp.description}</span>}
              </div>
            ))}
          </div>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            serial: {result.serial_number} · generated: {result.generated_at}
          </p>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-aibom {runId} --output aibom.json
          </p>
        </div>
      )}
    </section>
  );
}
