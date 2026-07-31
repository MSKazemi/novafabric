import { useState } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';
import { useLocalMru } from './useLocalMru';

// ---------- PII DEK crypto-shredding panel (nova pii erase — ADR-0069) ----------

export default function PiiErasePanel({ runIds }: { runIds: string[] }) {
  const [subjectId, setSubjectId] = useState('');
  const [retentionMonths, setRetentionMonths] = useState(6);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [noDek, setNoDek] = useState(false);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.piiErase>> | null>(null);
  const [mruSubjects, pushSubject] = useLocalMru('nova-pii-subjects');

  function handleErase() {
    const id = subjectId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setNoDek(false);
    setResult(null);
    api.piiErase(id, [], retentionMonths)
      .then(r => {
        if (!r.ok && r.error === 'no DEK found') {
          setNoDek(true);
        } else {
          pushSubject(id);
          setResult(r);
        }
      })
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  const receipt = result?.receipt as Record<string, unknown> | undefined;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">DEK Crypto-Shredding (nova pii erase) — ADR-0069</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            GDPR Art.17 erasure via AES-256-GCM DEK destruction — ciphertext becomes permanently unrecoverable
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0069
        </span>
      </div>

      <div className="flex gap-2 items-end flex-wrap">
        <div className="flex-1 min-w-[180px]">
          <label className="text-[9px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1 block">Subject ID</label>
          <SuggestInput
            value={subjectId}
            onChange={setSubjectId}
            suggestions={[...mruSubjects, ...runIds]}
            placeholder="data subject identifier"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <div className="w-44">
          <label className="text-[9px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1 block">
            Retention window (months, Art.17(3)(b))
          </label>
          <input
            type="number"
            min={1}
            value={retentionMonths}
            onChange={e => setRetentionMonths(Math.max(1, Number(e.target.value)))}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleErase}
          disabled={loading || !subjectId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-status-failure)] text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_18%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap self-end"
        >
          {loading ? 'Erasing…' : 'Erase DEK'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {noDek && (
        <div className="text-xs text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-warning)_25%,transparent)] rounded px-3 py-2">
          No DEK registered for this subject (subject may not have encrypted PII or was already erased)
        </div>
      )}

      {result?.ok && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wider"
              style={{
                color: result.erased ? 'var(--color-status-success)' : 'var(--color-status-warning)',
                background: result.erased
                  ? 'color-mix(in oklab, var(--color-status-success) 12%, transparent)'
                  : 'color-mix(in oklab, var(--color-status-warning) 12%, transparent)',
              }}
            >
              {result.erased ? 'Erased immediately' : 'Deferred'}
            </span>
            <CopyButton text={JSON.stringify(result.receipt, null, 2)} />
          </div>
          <div className="text-[10px] space-y-0.5 text-[var(--color-text-faint)]">
            {result.erased && receipt?.erased_at != null && (
              <div><span className="font-medium">Erased at:</span> <span className="font-mono">{String(receipt.erased_at)}</span></div>
            )}
            {!result.erased && receipt?.earliest_erasure_at != null && (
              <div><span className="font-medium">Deferred until:</span> <span className="font-mono">{String(receipt.earliest_erasure_at)}</span></div>
            )}
            {receipt?.proof_digest != null && (
              <div><span className="font-medium">Proof digest:</span> <span className="font-mono break-all">{String(receipt.proof_digest)}</span></div>
            )}
          </div>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova pii erase {subjectId}{retentionMonths !== 6 ? ` --retention-months ${retentionMonths}` : ''}
          </p>
        </div>
      )}
    </section>
  );
}
