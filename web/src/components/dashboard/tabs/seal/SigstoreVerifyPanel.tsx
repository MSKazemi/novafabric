// SigstoreVerifyPanel — mirrors `nova verify --backend sigstore` (ADR-0071).
// Extracted verbatim from SealTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

export default function SigstoreVerifyPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean; capsule_id: string; valid: boolean;
    identity: string | null; rekor_log_index: number | null; error: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notInstalled, setNotInstalled] = useState(false);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const verify = useCallback(async () => {
    const id = capsuleId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setNotInstalled(false);
    try {
      const res = await api.sigstoreVerify(id);
      setResult(res);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('501') || msg.toLowerCase().includes('not installed')) {
        setNotInstalled(true);
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [capsuleId]);

  const cliCmd = capsuleId.trim()
    ? `nova verify --backend sigstore --capsule-id ${capsuleId.trim()}`
    : 'nova verify --backend sigstore --capsule-id <capsule_id>';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        Sigstore Bundle Verify (nova verify --backend sigstore)
      </p>
      <p className="text-xs text-[var(--color-text-muted)]">
        Verify a stored Sigstore bundle — checks the Fulcio certificate, signature,
        and Rekor inclusion proof offline. (ADR-0071)
      </p>

      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule ID
          </label>
          <SuggestInput
            value={capsuleId}
            onChange={setCapsuleId}
            suggestions={runIds}
            placeholder="01KRK8..."
            className={inputClass}
            onEnter={verify}
          />
        </div>
        <button
          onClick={verify}
          disabled={loading || !capsuleId.trim()}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
            loading || !capsuleId.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'verifying…' : 'Verify Sigstore Bundle'}
        </button>
      </div>

      {/* CLI reference */}
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
        <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap">
          {cliCmd}
        </pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cliCmd} label="CLI" />
        </div>
      </div>

      {notInstalled && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] px-3 py-2">
          <p className="text-xs text-[var(--color-status-pending)] font-mono">
            novafabric[sigstore] not installed — run:{' '}
            <span className="font-bold">pip install novafabric[sigstore]</span>
          </p>
        </div>
      )}

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && !result.ok && result.error && result.error.includes('No Sigstore bundle') ? (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] px-3 py-2">
          <p className="text-xs text-[var(--color-status-pending)]">
            No Sigstore bundle found for this capsule
          </p>
        </div>
      ) : result && (
        <div
          className={clsx(
            'rounded border p-3 space-y-1.5',
            result.valid
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
          )}
        >
          <div className="flex items-center gap-2">
            <span
              className={clsx(
                'text-xs font-mono font-bold px-2 py-0.5 rounded',
                result.valid
                  ? 'bg-[color-mix(in_oklab,var(--color-status-success)_20%,transparent)] text-[var(--color-status-success)]'
                  : 'bg-[color-mix(in_oklab,var(--color-status-failure)_20%,transparent)] text-[var(--color-status-failure)]',
              )}
            >
              {result.valid ? 'VALID' : 'INVALID'}
            </span>
          </div>
          {result.identity !== null && (
            <p className="text-[11px] font-mono text-[var(--color-text-muted)]">
              identity: {result.identity}
            </p>
          )}
          {result.rekor_log_index !== null && (
            <p className="text-[11px] font-mono text-[var(--color-text-muted)]">
              rekor log index: {result.rekor_log_index}
            </p>
          )}
          {result.error && (
            <p className="text-[11px] text-[var(--color-status-failure)]">
              {result.error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
