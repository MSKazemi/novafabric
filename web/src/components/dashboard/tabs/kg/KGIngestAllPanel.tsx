// Re-ingest all capsules (nova kg ingest --all). Extracted verbatim from
// KGTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import CopyButton from '../../../ui/CopyButton';

export default function KGIngestAllPanel({ onDone }: { onDone: () => void }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    total?: number;
    newly_ingested?: number;
    skipped?: number;
    failed?: number;
    error?: string;
  } | null>(null);

  const handleIngestAll = useCallback(async () => {
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgIngestAll();
      setResult(r);
      if (r.ok) onDone();
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [onDone]);

  const cliCmd = 'nova kg ingest --all';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Re-ingest All Capsules</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            nova kg ingest --all — scans the server&apos;s capsule directory and ingests any capsules not yet in the KG
          </p>
        </div>
        <button
          onClick={handleIngestAll}
          disabled={loading}
          className={clsx(
            'text-xs font-mono px-3 py-1.5 rounded border transition-colors shrink-0',
            loading
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'ingesting…' : 'Re-ingest All'}
        </button>
      </div>

      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono space-y-0.5',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)]',
        )}>
          {result.ok ? (
            <>
              <div className="text-[var(--color-status-success)]">
                ✓ {result.total} capsule(s) scanned · {result.newly_ingested} newly ingested
              </div>
              <div className="text-[var(--color-text-faint)] text-[10px]">
                skipped (already tracked): {result.skipped ?? 0} · failed: {result.failed ?? 0}
              </div>
            </>
          ) : (
            <span className="text-[var(--color-status-failure)]">✗ {result.error}</span>
          )}
        </div>
      )}

      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[10px] font-mono text-[var(--color-text-muted)]">{cliCmd}</pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
      </div>
    </section>
  );
}
