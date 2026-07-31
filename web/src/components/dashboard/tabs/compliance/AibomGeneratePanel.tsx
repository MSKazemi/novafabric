import { useState } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';

// ---------- AIBOM generate panel (nova aibom generate — cap-008) ----------

export default function AibomGeneratePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [all, setAll] = useState(false);
  const [force, setForce] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.aibomGenerate>> | null>(null);

  function handleGenerate() {
    const id = runId.trim();
    if (!all && !id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.aibomGenerate(all ? { all: true, force } : { run_id: id, force })
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Generate AI-SBOM</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Write CycloneDX 1.7 aibom.json per capsule (EU CRA, deadline 2026-09-11) — <code className="font-mono">nova aibom generate</code>
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
            disabled={all}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full disabled:opacity-40 disabled:cursor-not-allowed"
          />
        </div>
        <button
          type="button"
          onClick={handleGenerate}
          disabled={loading || (!all && !runId.trim())}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Generating…' : 'Generate'}
        </button>
      </div>

      <div className="flex gap-4">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={all}
            onChange={e => setAll(e.target.checked)}
            className="rounded"
          />
          <span className="text-xs text-[var(--color-text-muted)]">All capsules</span>
        </label>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={force}
            onChange={e => setForce(e.target.checked)}
            className="rounded"
          />
          <span className="text-xs text-[var(--color-text-muted)]">Force regenerate</span>
        </label>
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
              <div className="text-lg font-mono font-bold" style={{ color: 'var(--color-status-success)' }}>
                {result.written}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">written</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div className="text-lg font-mono font-bold text-[var(--color-text-faint)]">{result.skipped}</div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">skipped</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div
                className="text-lg font-mono font-bold"
                style={{ color: result.failed > 0 ? 'var(--color-status-failure)' : 'var(--color-text-faint)' }}
              >
                {result.failed}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">failed</div>
            </div>
          </div>

          {result.path && (
            <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
              path: {result.path}
            </p>
          )}

          {result.errors && result.errors.length > 0 && (
            <div className="space-y-1">
              {result.errors.map((err, i) => (
                <div
                  key={i}
                  className="text-[10px] font-mono text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] rounded px-2 py-1"
                >
                  {err}
                </div>
              ))}
            </div>
          )}

          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova aibom generate{all ? ' --all' : runId.trim() ? ` ${runId.trim()}` : ''}{force ? ' --force' : ''}
          </p>
        </div>
      )}
    </section>
  );
}
