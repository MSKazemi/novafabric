// Ingest a capsule into the KG (nova kg ingest). Extracted verbatim from
// KGTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';

export default function KGIngestPanel({ capsuleDirs }: { capsuleDirs: string[] }) {
  const [capsulePath, setCapsulePath] = useState('');
  const [verified, setVerified] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; ingested?: number; written?: number; skipped?: number; error?: string } | null>(null);

  const inputClass = 'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const handleIngest = useCallback(async () => {
    if (!capsulePath.trim()) return;
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgIngest(capsulePath.trim(), verified);
      setResult(r);
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [capsulePath, verified]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Ingest Capsule into KG</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg ingest — reads model-calls.jsonl + tool-calls.jsonl from a capsule directory
        </p>
      </div>

      <div className="space-y-2">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Capsule path</label>
          <SuggestInput
            value={capsulePath}
            onChange={setCapsulePath}
            suggestions={capsuleDirs}
            placeholder="/data/nova/capsules/01KRK8H9... or run_id"
            className={inputClass}
            onEnter={handleIngest}
          />
        </div>
        <label className="flex items-center gap-2 text-xs text-[var(--color-text-muted)] cursor-pointer">
          <input type="checkbox" checked={verified} onChange={(e) => setVerified(e.target.checked)} className="rounded" />
          Mark events as NovaSeal-verified (confidence = 1.0)
        </label>
      </div>

      <button
        onClick={handleIngest}
        disabled={loading || !capsulePath.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !capsulePath.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'ingesting…' : 'Ingest'}
      </button>

      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)]',
        )}>
          {result.ok
            ? <span className="text-[var(--color-status-success)]">✓ Ingested {result.ingested} events → wrote {result.written} KG edges{result.skipped ? ` (${result.skipped} skipped)` : ''}</span>
            : <span className="text-[var(--color-status-failure)]">✗ {result.error}</span>
          }
        </div>
      )}
    </section>
  );
}
