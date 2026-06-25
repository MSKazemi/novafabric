import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../../../lib/api';
import type { EvidenceSummary } from '../../../lib/api';
import EvidenceList from '../../evidence/EvidenceList';
import EvidenceDetailPanel from '../../evidence/EvidenceDetail';
import type { Tab } from '../Sidebar';
import ActionButton from '../../ui/ActionButton';
import { useMutation } from '../../../lib/useMutation';

/** Audit-grade evidence assertions (nova evidence, ADR-0087). */
function AssertionsPanel() {
  const [runId, setRunId] = useState('');
  const completeness = useMutation((rid: string) => api.evidenceCompleteness(rid), { silentSuccess: true });
  const bind = useMutation((rid: string) => api.evidenceBind(rid), { silentSuccess: true });
  const rid = runId.trim();
  return (
    <div className="rounded border border-[var(--color-border)] p-4 space-y-2">
      <h3 className="text-sm font-medium text-[var(--color-text)]">Evidence assertions <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">ADR-0087 · experimental</span></h3>
      <p className="text-xs text-[var(--color-text-muted)]">Compute a completeness assertion or criterion-evidence bindings for a capture.</p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
          Run ID
          <input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="01JABC…" className="w-56 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]" />
        </label>
        <ActionButton onClick={() => completeness.run(rid)} pending={completeness.pending} disabled={!rid} variant="primary">Completeness</ActionButton>
        <ActionButton onClick={() => bind.run(rid)} pending={bind.pending} disabled={!rid}>Bind (NIST AI RMF)</ActionButton>
      </div>
      {completeness.result?.assertion && (
        <pre className="whitespace-pre-wrap font-mono text-[10px] text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] rounded p-2 max-h-48 overflow-auto">{JSON.stringify(completeness.result.assertion, null, 2)}</pre>
      )}
      {bind.result?.ok && (
        <div className="text-xs text-[var(--color-text-muted)]">
          {bind.result.count} bindings against <code className="font-mono">{bind.result.profile}</code>
        </div>
      )}
    </div>
  );
}

interface Props {
  onNavigate?: (tab: Tab) => void;
  onCountChange?: (n: number) => void;
}

export default function EvidenceTab({ onNavigate, onCountChange }: Props) {
  const [bundles, setBundles] = useState<EvidenceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const onCountChangeRef = useRef(onCountChange);
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    api.listEvidence()
      .then((data) => {
        setBundles(data.bundles);
        onCountChangeRef.current?.(data.count);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []); // onCountChange intentionally excluded — accessed via ref

  useEffect(() => { refresh(); }, [refresh]);

  // 'r' key shortcut to refresh (consistent with other tabs)
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'r' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const tag = (e.target as HTMLElement).tagName;
        if (tag !== 'INPUT' && tag !== 'TEXTAREA') refresh();
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [refresh]);

  if (loading && bundles.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-[var(--color-text-faint)] text-sm">
        Loading evidence bundles…
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 text-sm space-y-2">
        <p className="text-[var(--color-status-failure)]">Failed to load evidence bundles: {error}</p>
        <button
          onClick={refresh}
          className="text-xs px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  if (selectedId) {
    return (
      <EvidenceDetailPanel
        bundleId={selectedId}
        onBack={() => setSelectedId(null)}
      />
    );
  }

  return (
    <div className="space-y-4">
      <EvidenceList
        bundles={bundles}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onRefresh={refresh}
        onNavigate={onNavigate}
      />
      <AssertionsPanel />
    </div>
  );
}
