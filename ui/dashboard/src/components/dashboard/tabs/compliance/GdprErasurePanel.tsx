import { useState, useCallback } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import { useLocalMru } from './useLocalMru';

// ---------- DB-ERA-1: GDPR erasure panel (v0.18.0, cap-003) ----------

export default function GdprErasurePanel({ runIds }: { runIds: string[] }) {
  const [subjectId, setSubjectId] = useState('');
  const [reason, setReason] = useState('gdpr_art_17');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<{ state: string; erased: boolean; cap003_enabled: boolean; note: string } | null>(null);
  const [erasureSuggestions, pushErasureSubject] = useLocalMru('nova-erasure-subjects');

  const [statusSubjectId, setStatusSubjectId] = useState('');
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusResult, setStatusResult] = useState<{ cap003_enabled: boolean; crypto_shred_available?: boolean; requests: Array<Record<string, unknown>> } | null>(null);

  const checkStatus = useCallback(async () => {
    setStatusLoading(true);
    setErr(null);
    try { setStatusResult(await api.erasureStatus(statusSubjectId.trim() || undefined)); }
    catch (e) { setErr((e as Error).message); }
    finally { setStatusLoading(false); }
  }, [statusSubjectId]);

  const submit = useCallback(async () => {
    const id = subjectId.trim();
    if (!id) return;
    setSubmitting(true);
    setErr(null);
    try {
      // ADR-0210: real execution — confirmed:true, terminal state + receipt hash.
      const r = await api.erasureRequest(id, reason);
      setResult({
        state: r.request.state,
        erased: r.erased,
        cap003_enabled: r.cap003_enabled,
        // A FAILED request still carries a receipt hash; show *why* it failed first,
        // otherwise the reason (e.g. subject_not_found) is hidden behind a hash.
        note: r.request.state === 'FAILED' && (r.request.error_class || r.request.error_detail)
          ? `${r.request.error_class ?? 'error'}: ${r.request.error_detail ?? ''}`.replace(/"/g, '')
          : r.request.receipt_sha256
          ? `receipt_sha256: ${r.request.receipt_sha256}`
          : (r.request.error_detail ?? ''),
      });
      pushErasureSubject(id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [subjectId, reason, pushErasureSubject]);

  const stateColor: Record<string, string> = {
    PENDING: 'text-[var(--color-status-pending)]',
    COMPLETED: 'text-[var(--color-status-success)]',
    DEFERRED: 'text-[var(--color-status-pending)]',
    FAILED: 'text-[var(--color-status-failure)]',
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">GDPR Erasure Request</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-003 · Art. 17 right-to-be-forgotten · dual-object split (audit/PII)
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
          ADR-0066
        </span>
      </div>

      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2 font-mono">{err}</div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Subject ID (or Run ID)</label>
          <SuggestInput
            value={subjectId}
            onChange={setSubjectId}
            suggestions={[...new Set([...runIds, ...erasureSuggestions])]}
            onEnter={submit}
            placeholder="subject-001 or run_..."
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono w-full"
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Reason</label>
          <select
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono w-full"
          >
            <option value="gdpr_art_17">gdpr_art_17 (right-to-be-forgotten)</option>
            <option value="dsar">dsar (data subject access request)</option>
            <option value="internal">internal</option>
          </select>
        </div>
      </div>

      <button
        onClick={submit}
        disabled={submitting || !subjectId.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          submitting || !subjectId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-[var(--color-accent-fg)]',
        )}
      >
        {submitting ? 'submitting…' : 'Queue erasure request'}
      </button>

      {result && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 space-y-1.5">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">state</span>
            <span className={clsx('text-xs font-mono font-bold', stateColor[result.state] ?? 'text-[var(--color-text)]')}>{result.state}</span>
          </div>
          {!result.erased && (
            <p className="text-[10px] text-[var(--color-status-pending)]">Not erased yet — the request is {result.state}.</p>
          )}
          <p className="text-[10px] text-[var(--color-text-muted)]">{result.note}</p>
        </div>
      )}

      <div className="border-t border-[var(--color-border)] pt-3 space-y-2">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Check Erasure Status</p>
        <p className="text-[10px] text-[var(--color-text-faint)]">GET /api/compliance/erasure/status — the persisted queue (ADR-0210). There is no CLI equivalent: nova erasure status is not implemented.</p>
        <div className="flex gap-2">
          <SuggestInput
            value={statusSubjectId}
            onChange={setStatusSubjectId}
            suggestions={erasureSuggestions}
            onEnter={checkStatus}
            placeholder="subject-001 (leave blank for all)"
            className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
          <button
            onClick={checkStatus}
            disabled={statusLoading}
            className={clsx(
              'text-xs font-mono px-3 py-1.5 rounded border transition-colors',
              statusLoading
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-[var(--color-accent-fg)]',
            )}
          >
            {statusLoading ? '…' : 'Check Status'}
          </button>
        </div>
        {statusResult && (
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-xs font-mono space-y-1">
            <div>crypto-shred: {statusResult.crypto_shred_available === false ? 'unavailable (DEK store cannot be loaded)' : 'available'}</div>
            <div className="text-[var(--color-text-faint)]">cap-003 dual-object split (separate capability, does not gate erasure): {statusResult.cap003_enabled ? 'enabled' : 'disabled'}</div>
            <div>Requests: {statusResult.requests.length}</div>
            {statusResult.requests.length > 0 && (
              <div className="space-y-0.5 mt-1">
                {statusResult.requests.map((r, i) => (
                  <div key={i} className="text-[10px] text-[var(--color-text-muted)]">
                    {JSON.stringify(r)}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
