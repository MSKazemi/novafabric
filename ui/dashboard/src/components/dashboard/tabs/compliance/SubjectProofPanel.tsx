import { useState, useCallback } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import { useLocalMru } from './useLocalMru';

// ---------- Subject Proof panel ----------

export default function SubjectProofPanel() {
  const [subjectId, setSubjectId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [subjectSuggestions, pushSubjectId] = useLocalMru('nova-subject-proof-ids');

  const run = useCallback(async () => {
    const id = subjectId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.subjectProof(id);
      setResult(res);
      pushSubjectId(id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [subjectId, pushSubjectId]);

  const downloadJson = useCallback(() => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `subject-proof-${subjectId || 'report'}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result, subjectId]);

  const records = Array.isArray(result?.records) ? result!.records as unknown[] : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">GDPR Subject Proof</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-001 · Art. 17 Right to Erasure · HMAC-SHA256 lookup in redaction index
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-001</span>
      </div>

      <div className="flex gap-2">
        <SuggestInput
          value={subjectId}
          onChange={setSubjectId}
          suggestions={subjectSuggestions}
          onEnter={run}
          placeholder="data subject identifier (e.g. user@example.com)"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <button
          onClick={run}
          disabled={loading || !subjectId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? '…' : 'Lookup'}
        </button>
      </div>

      <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
        $ nova subject-proof {subjectId || '<subject_id>'}
      </div>

      <div className="text-[10px] text-[var(--color-text-faint)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] border border-[color-mix(in_oklab,var(--color-status-pending)_20%,transparent)] rounded px-2.5 py-1.5">
        Requires <code className="font-mono">NOVA_PII_PEPPER</code> env var set on the server. Subject ID is never stored — only an HMAC is queried.
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              {records !== null ? `${records.length} redaction record${records.length !== 1 ? 's' : ''}` : 'Proof ready'}
            </span>
            <button
              onClick={downloadJson}
              className="text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
            >
              ↓ Download JSON
            </button>
          </div>
          {records !== null && records.length === 0 && (
            <p className="text-xs text-[var(--color-text-faint)] italic py-2 text-center">
              No redaction records found for this subject — either not captured or already erased.
            </p>
          )}
          {records !== null && records.length > 0 && (
            <div className="max-h-48 overflow-y-auto space-y-1 pr-1">
              {(records as Array<Record<string, unknown>>).map((r, i) => (
                <div key={i} className="text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 space-y-0.5">
                  <div className="font-mono text-[var(--color-text)] truncate">{String(r.capsule_id)}</div>
                  <div className="text-[var(--color-text-faint)] flex gap-3">
                    <span>field: <span className="font-mono">{String(r.field_path)}</span></span>
                    <span>basis: {String(r.legal_basis)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
