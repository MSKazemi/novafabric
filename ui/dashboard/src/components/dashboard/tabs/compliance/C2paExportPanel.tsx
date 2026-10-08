import { useState } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

// ---------- C2PA manifest export panel (nova export-c2pa) ----------

export default function C2paExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [includeTrainingMining, setIncludeTrainingMining] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    run_id: string;
    manifest: object;
    note: string;
  } | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportC2pa(id, includeTrainingMining)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">C2PA Manifest Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export C2PA v2.3 content provenance manifest — mirrors <code className="font-mono">nova export-c2pa</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0074
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
          {loading ? 'Exporting…' : 'Export C2PA Manifest'}
        </button>
      </div>

      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={includeTrainingMining}
          onChange={e => setIncludeTrainingMining(e.target.checked)}
          className="rounded"
        />
        <span className="text-xs text-[var(--color-text-muted)]">Include training-mining:notAllowed assertion</span>
      </label>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        C2PA v2.3 — ADR-0074 / EU AI Act Art.50 · deadline 2026-08-02
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">run_id: <span className="font-mono">{result.run_id}</span></span>
            <CopyButton text={JSON.stringify(result.manifest, null, 2)} />
          </div>
          <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded p-2 overflow-auto max-h-64 whitespace-pre-wrap break-all">
            {JSON.stringify(result.manifest, null, 2)}
          </pre>
          {result.note && (
            <p className="text-[10px] text-[var(--color-text-faint)]">{result.note}</p>
          )}
        </div>
      )}
    </section>
  );
}
