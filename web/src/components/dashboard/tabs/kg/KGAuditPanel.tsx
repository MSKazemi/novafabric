// KG audit health check (nova kg audit). Extracted verbatim from KGTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';

export default function KGAuditPanel() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    store_health?: string;
    node_count?: number;
    edge_count?: number;
    orphaned_edges?: number;
    zero_call_count?: number;
    issues?: string[];
    error?: string;
  } | null>(null);

  const handleAudit = useCallback(async () => {
    setLoading(true);
    setResult(null);
    try {
      setResult(await api.kgAudit());
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">KG Audit</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg audit — health check: orphaned edges, zero-call nodes, store integrity
        </p>
      </div>
      <button
        onClick={handleAudit}
        disabled={loading}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'auditing…' : 'Run Audit'}
      </button>
      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono space-y-1',
          result.ok && (!result.issues || result.issues.length === 0)
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-warning,var(--color-status-pending))_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-pending)_25%,transparent)]',
        )}>
          {result.ok ? (
            <>
              <div>Health: <span className="text-[var(--color-status-success)]">{result.store_health}</span></div>
              <div>Nodes: {result.node_count ?? '—'} | Edges: {result.edge_count ?? '—'}</div>
              <div>Orphaned: {result.orphaned_edges ?? 0} | Zero-call: {result.zero_call_count ?? 0}</div>
              {result.issues && result.issues.length > 0
                ? <div className="text-[var(--color-status-pending)]">Issues: {result.issues.join('; ')}</div>
                : <div className="text-[var(--color-status-success)]">✓ No issues found</div>
              }
            </>
          ) : (
            <span className="text-[var(--color-status-failure)]">✗ {result.error ?? result.store_health}</span>
          )}
        </div>
      )}
    </section>
  );
}
