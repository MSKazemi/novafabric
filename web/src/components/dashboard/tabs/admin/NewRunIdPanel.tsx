// New Run ID generator (cap-007, FR-27). Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import CopyButton from '../../../ui/CopyButton';
import { Panel, SectionHeading } from './helpers';

export default function NewRunIdPanel() {
  const [runId, setRunId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await api.newRunId();
      setRunId(r.run_id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <Panel>
      <SectionHeading>New Run ID</SectionHeading>
      <div className="space-y-3">
        <p className="text-xs text-[var(--color-text-muted)]">
          Generate a fresh ULID to use as <code className="font-mono text-[10px]">NOVAFABRIC_GLOBAL_RUN_ID</code> before
          running a distributed job (cap-007, FR-27).
        </p>
        <div className="flex items-center gap-2">
          <button
            onClick={generate}
            disabled={loading}
            className={clsx(
              'text-xs font-mono px-3 py-1.5 rounded border transition-colors',
              loading
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {loading ? 'generating…' : 'Generate'}
          </button>
          {runId && (
            <div className="flex items-center gap-1.5 flex-1 min-w-0">
              <code className="text-xs font-mono px-2 py-1 rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-accent)] truncate">
                {runId}
              </code>
              <CopyButton text={runId} label="ID" />
              <CopyButton text={`NOVAFABRIC_GLOBAL_RUN_ID=${runId} nova capture`} label="ENV" />
            </div>
          )}
        </div>
        {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      </div>
    </Panel>
  );
}
