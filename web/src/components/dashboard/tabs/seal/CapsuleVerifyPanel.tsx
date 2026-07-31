// CapsuleVerifyPanel — mirrors `nova verify <capsule>` (ADR-0041). Extracted
// verbatim from SealTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api, type CapsuleVerifyResult } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

function CheckRow({ label, ok }: { label: string; ok: boolean | undefined }) {
  if (ok === undefined) return null;
  return (
    <div className="flex items-center gap-2 text-[11px] font-mono">
      <span className={ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>
        {ok ? '✓' : '✗'}
      </span>
      <span className={ok ? 'text-[var(--color-text-muted)]' : 'text-[var(--color-text)]'}>{label}</span>
      <span className={clsx(
        'ml-auto font-medium',
        ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
      )}>
        {ok ? 'OK' : 'FAIL'}
      </span>
    </div>
  );
}

export default function CapsuleVerifyPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<CapsuleVerifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const verify = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.capsuleVerify(id);
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [runId]);

  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full';

  const cliCmd = runId.trim() ? `nova verify ${runId.trim()}` : 'nova verify <capsule_dir>';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        Capsule Integrity Verify
      </p>
      <p className="text-xs text-[var(--color-text-muted)]">
        Verify a capsule's cryptographic seal — DSSE signature (ECDSA P-256), RFC 3161 timestamp, and
        Merkle log inclusion. Requires NovaSeal to be configured. (ADR-0041)
      </p>

      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Run ID
          </label>
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="01KRK8..."
            className={inputClass}
            onEnter={verify}
          />
        </div>
        <button
          onClick={verify}
          disabled={loading || !runId.trim()}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
            loading || !runId.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'verifying…' : 'Verify capsule'}
        </button>
      </div>

      {/* CLI reference */}
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
        <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap">{cliCmd}</pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cliCmd} label="CLI" />
        </div>
      </div>

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && (
        <div className={clsx(
          'rounded border p-3 space-y-2',
          result.sealed === false
            ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
            : result.configured === false
              ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
              : result.valid
                ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
                : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
        )}>
          {result.sealed === false ? (
            <p className="text-xs text-[var(--color-status-pending)]">Not sealed — {result.message}</p>
          ) : result.configured === false ? (
            <p className="text-xs text-[var(--color-status-pending)]">{result.message}</p>
          ) : (
            <>
              <div className="space-y-1">
                <CheckRow label="Signature (DSSE ECDSA P-256)" ok={result.signature_ok} />
                <CheckRow label="Timestamp (RFC 3161)" ok={result.timestamp_ok} />
                <CheckRow label="Merkle log inclusion" ok={result.log_integrity_ok} />
              </div>
              {result.capsule_id && (
                <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
                  capsule_id: {result.capsule_id}
                </p>
              )}
              {result.errors && result.errors.length > 0 && (
                <ul className="text-[10px] text-[var(--color-status-failure)] space-y-0.5 pt-1 border-t border-[color-mix(in_oklab,var(--color-status-failure)_20%,transparent)]">
                  {result.errors.map((e, i) => <li key={i}>✗ {e}</li>)}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
