// DB-STG-1: Storage operations card (v0.18.0, cap-003/009). Extracted
// verbatim from InfraTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';

export default function StorageOpsCard({ runIds }: { runIds: string[] }) {
  const [endpoint, setEndpoint] = useState('');
  const [bucket, setBucket] = useState('nova-capsules');
  const [valLoading, setValLoading] = useState(false);
  const [valResult, setValResult] = useState<{ ok: boolean; result?: Record<string, unknown>; error?: string; error_class?: string } | null>(null);

  const [runId, setRunId] = useState('');
  const [inspLoading, setInspLoading] = useState(false);
  const [inspResult, setInspResult] = useState<{ audit_object_key: string; pii_object_key: string | null; cap003_enabled: boolean; note: string } | null>(null);
  const [inspErr, setInspErr] = useState<string | null>(null);

  const validate = useCallback(async () => {
    setValLoading(true);
    setValResult(null);
    try {
      const r = await api.storageValidate(endpoint || undefined, bucket || 'nova-capsules');
      setValResult(r);
    } catch (e) {
      setValResult({ ok: false, error: (e as Error).message });
    } finally {
      setValLoading(false);
    }
  }, [endpoint, bucket]);

  const inspect = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setInspLoading(true);
    setInspErr(null);
    setInspResult(null);
    try {
      const r = await api.storageInspect(id);
      setInspResult(r);
    } catch (e) {
      setInspErr((e as Error).message);
    } finally {
      setInspLoading(false);
    }
  }, [runId]);

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Storage Operations (cap-003 / cap-009)</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            S3 Object-Lock COMPLIANCE validation · dual-object split inspection
          </p>
        </div>
        <span className="text-2xs font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
          ADR-0066
        </span>
      </div>

      {/* Validate panel */}
      <div className="space-y-2 border-b border-[var(--color-border)] pb-3">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Validate S3 Object Lock</p>
        <div className="grid grid-cols-2 gap-2">
          <input
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="https://s3.amazonaws.com (optional)"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
          <input
            value={bucket}
            onChange={(e) => setBucket(e.target.value)}
            placeholder="nova-capsules"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
        </div>
        <button
          onClick={validate}
          disabled={valLoading}
          className="text-xs font-mono px-3 py-1 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white transition-colors disabled:opacity-50"
        >
          {valLoading ? 'validating…' : 'Validate'}
        </button>
        {valResult && (
          <div className={clsx(
            'text-[11px] font-mono rounded border px-3 py-2',
            valResult.ok
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]'
          )}>
            {valResult.ok ? 'OK — ' + JSON.stringify(valResult.result) : (valResult.error_class || 'error') + ': ' + valResult.error}
          </div>
        )}
      </div>

      {/* Inspect panel */}
      <div className="space-y-2">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Inspect dual-object split for run</p>
        <div className="flex gap-2">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={inspect}
            placeholder="run_2026_..."
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono flex-1"
          />
          <button
            onClick={inspect}
            disabled={inspLoading || !runId.trim()}
            className="text-xs font-mono px-3 py-1 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white transition-colors disabled:opacity-50"
          >
            {inspLoading ? 'inspecting…' : 'Inspect'}
          </button>
        </div>
        {inspErr && (
          <div className="text-[11px] font-mono text-[var(--color-status-failure)]">{inspErr}</div>
        )}
        {inspResult && (
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 space-y-1 text-[11px] font-mono">
            <div><span className="text-[var(--color-text-faint)]">audit:</span> {inspResult.audit_object_key}</div>
            <div>
              <span className="text-[var(--color-text-faint)]">pii:</span>{' '}
              {inspResult.pii_object_key ?? <span className="text-[var(--color-text-faint)]">(none — NOVA_CAP003_ENABLED=false)</span>}
            </div>
            <div className="text-[10px] text-[var(--color-text-muted)] pt-1 border-t border-[var(--color-border)] mt-1">{inspResult.note}</div>
          </div>
        )}
      </div>
    </div>
  );
}
