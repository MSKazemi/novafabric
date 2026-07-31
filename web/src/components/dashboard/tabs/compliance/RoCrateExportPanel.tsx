import { useState } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';

// ---------- RO-Crate export panel (nova export-rocrate) ----------

export default function RoCrateExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    run_id: string;
    filename: string;
    zip_base64: string;
    size_bytes: number;
    note: string;
  } | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportRoCrate(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  function handleDownload() {
    if (!result) return;
    const blob = new Blob([Uint8Array.from(atob(result.zip_base64), c => c.charCodeAt(0))], { type: 'application/zip' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = result.filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">RO-Crate Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export W3C RO-Crate v1.1 archive — mirrors <code className="font-mono">nova export-rocrate</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0073
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
          {loading ? 'Exporting…' : 'Export RO-Crate'}
        </button>
      </div>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        W3C RO-Crate v1.1 — portable, interoperable capsule archive
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-[10px] text-[var(--color-text-faint)] font-mono">{result.filename}</span>
            <span className="text-[10px] text-[var(--color-text-faint)]">{(result.size_bytes / 1024).toFixed(1)} KB</span>
            <button
              type="button"
              onClick={handleDownload}
              className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] transition-colors"
            >
              Download ZIP
            </button>
          </div>
          {result.note && (
            <p className="text-[10px] text-[var(--color-text-faint)]">{result.note}</p>
          )}
        </div>
      )}
    </section>
  );
}
