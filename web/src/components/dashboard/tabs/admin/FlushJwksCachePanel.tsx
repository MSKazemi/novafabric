// OIDC JWKS cache flush (v0.27.0). Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { CliRefRow, Panel, SectionHeading } from './helpers';

export default function FlushJwksCachePanel() {
  const [result, setResult] = useState<{ ok: boolean; note: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleFlush = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.flushJwksCache();
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <Panel>
      <SectionHeading>OIDC Configuration</SectionHeading>
      <div className="space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-medium text-[var(--color-text)] mb-0.5">Flush JWKS Cache</p>
            <p className="text-xs text-[var(--color-text-muted)]">
              Force the running server to re-fetch its JWKS from the OIDC provider. Use after
              rotating keys or when auth failures suggest a stale cache.
            </p>
          </div>
          <button
            onClick={handleFlush}
            disabled={loading}
            className="shrink-0 text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
          >
            {loading ? 'Flushing…' : 'Flush cache'}
          </button>
        </div>
        {result && (
          <div className={clsx(
            'rounded border px-3 py-2 text-xs font-mono',
            result.ok
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]'
              : 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] text-[var(--color-status-pending)]',
          )}>
            {result.note}
          </div>
        )}
        {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
        <CliRefRow cmd="nova server flush-jwks-cache --server-url <url>" label="CLI reference" />
      </div>
    </Panel>
  );
}
