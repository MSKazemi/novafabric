import { useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

// ---------- GDPR RoPA export panel (nova export-ropa — cap-007) ----------

export default function RoPAExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [controllerName, setControllerName] = useState('');
  const [controllerContact, setControllerContact] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportRopa>> | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportRopa(id, controllerName.trim(), controllerContact.trim())
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">GDPR Art.30 RoPA Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export Records of Processing Activities — mirrors <code className="font-mono">nova export-ropa</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          cap-007
        </span>
      </div>

      <div className="space-y-2">
        <SuggestInput
          value={runId}
          onChange={setRunId}
          suggestions={runIds}
          placeholder="run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
        />
        <input
          type="text"
          value={controllerName}
          onChange={e => setControllerName(e.target.value)}
          placeholder="Controller name (GDPR Art.30(1)(a)) — optional"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 focus:border-[var(--color-accent)] focus:outline-none w-full"
        />
        <input
          type="text"
          value={controllerContact}
          onChange={e => setControllerContact(e.target.value)}
          placeholder="Controller contact details — optional"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 focus:border-[var(--color-accent)] focus:outline-none w-full"
        />
      </div>

      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] text-[var(--color-text-faint)]">
          GDPR Art.30 · cap-007 · Missing fields marked OPERATOR_DECLARED_REQUIRED
        </p>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Exporting…' : 'Export RoPA'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <span className={clsx(
              'text-[10px] font-semibold px-1.5 py-0.5 rounded',
              result.completeness === 'complete'
                ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                : 'text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_10%,transparent)]',
            )}>
              {result.completeness}
            </span>
            {result.missing_fields.length > 0 && (
              <span className="text-[10px] text-[var(--color-text-faint)]">
                missing: {result.missing_fields.join(', ')}
              </span>
            )}
            <CopyButton text={JSON.stringify(result.document, null, 2)} />
          </div>
          <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded p-2 overflow-auto max-h-64 whitespace-pre-wrap break-all">
            {JSON.stringify(result.document, null, 2)}
          </pre>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-ropa {runId} --output ropa.json{controllerName ? ` --controller-name "${controllerName}"` : ''}
          </p>
        </div>
      )}
    </section>
  );
}
