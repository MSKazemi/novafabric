// Merkle Log Verify panel (ADR-0041). Extracted verbatim from SealTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

interface MerkleLogResult {
  consistent: boolean;
  entry_count: number;
  message: string;
  capsule_included?: boolean;
}

export default function MerkleLogVerifyPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<MerkleLogResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const verify = useCallback(async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.sealLogVerify(capsuleId.trim() || undefined);
      setResult(res as MerkleLogResult);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [capsuleId]);

  const cliCmd = capsuleId.trim()
    ? `nova seal log verify --capsule-id ${capsuleId.trim()}`
    : 'nova seal log verify';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        Merkle Log Verify
      </p>
      <p className="text-xs text-[var(--color-text-muted)]">
        Verify Merkle log consistency — checks all entries are cryptographically
        linked and none have been tampered with. (ADR-0041)
      </p>

      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule ID (optional)
          </label>
          <SuggestInput
            value={capsuleId}
            onChange={setCapsuleId}
            suggestions={runIds}
            placeholder="leave blank to verify full log"
            className={inputClass}
            onEnter={verify}
          />
        </div>
        <button
          onClick={verify}
          disabled={loading}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
            loading
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'verifying…' : 'Verify log integrity'}
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

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && (
        <div
          className={clsx(
            'rounded border p-3 space-y-2',
            result.consistent
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
          )}
        >
          <div className="flex items-center gap-3">
            <span
              className={clsx(
                'text-xs font-mono font-bold',
                result.consistent ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
              )}
            >
              {result.consistent ? '✓ Log consistent' : '✗ Log inconsistent'}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.entry_count} entr{result.entry_count === 1 ? 'y' : 'ies'}
            </span>
            {result.capsule_included !== undefined && (
              <span
                className={clsx(
                  'text-[10px] font-mono',
                  result.capsule_included
                    ? 'text-[var(--color-status-success)]'
                    : result.entry_count === 0
                      ? 'text-[var(--color-text-faint)]'
                      : 'text-[var(--color-status-failure)]',
                )}
              >
                {result.capsule_included
                  ? 'capsule: included'
                  : result.entry_count === 0
                    ? 'seal log is empty'
                    : 'capsule: not found'}
              </span>
            )}
          </div>
          {result.message && (
            <p className="text-xs text-[var(--color-text-muted)]">{result.message}</p>
          )}
        </div>
      )}
    </div>
  );
}
