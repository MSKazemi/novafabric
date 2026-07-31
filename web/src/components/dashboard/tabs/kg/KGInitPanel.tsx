// Initialise KG schema (nova kg init). Extracted verbatim from KGTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';

export default function KGInitPanel({ onDone }: { onDone: () => void }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; db_path?: string; note?: string; error?: string } | null>(null);

  const handleInit = useCallback(async () => {
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgInit();
      setResult(r);
      if (r.ok) onDone();
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [onDone]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Initialise KG Schema</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            nova kg init — idempotent DDL setup (safe to re-run)
          </p>
        </div>
        <button
          onClick={handleInit}
          disabled={loading}
          className={clsx(
            'text-xs font-mono px-3 py-1.5 rounded border transition-colors shrink-0',
            loading
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'initialising…' : 'Init schema'}
        </button>
      </div>

      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)] text-[var(--color-status-success)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] text-[var(--color-status-failure)]',
        )}>
          {result.ok ? `✓ ${result.note}` : `✗ ${result.error}`}
          {result.db_path && <div className="text-[10px] text-[var(--color-text-faint)] mt-1">{result.db_path}</div>}
        </div>
      )}
    </section>
  );
}
