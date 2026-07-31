// Reindex capsules (nova ingest-capsule, v0.46.0). Extracted verbatim from
// AdminTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { api } from '../../../../lib/api';
import { CliRefRow, Panel, SectionHeading } from './helpers';

export default function IngestCapsulePanel() {
  const inputClass =
    'w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)] disabled:opacity-50';
  const labelClass = 'block text-[10px] font-mono text-[var(--color-text-faint)] mb-1';

  const [runId, setRunId] = useState('');
  const [allMode, setAllMode] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleIngest = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!allMode && !runId.trim()) return;
    setLoading(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await api.ingestCapsule(allMode ? { all: true } : { run_id: runId.trim() });
      if (allMode) {
        setMsg(`indexed ${r.indexed ?? 0} capsule(s)`);
      } else {
        setMsg(`run ${runId.trim()} indexed ${r.is_new ? '(new)' : '(updated)'}`);
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [allMode, runId]);

  return (
    <Panel>
      <SectionHeading>Reindex Capsules</SectionHeading>
      <div className="space-y-3">
        <p className="text-xs text-[var(--color-text-muted)]">
          Index capsule files into the runs metadata store — <code className="font-mono text-[10px]">nova ingest-capsule</code>
        </p>
        <form onSubmit={handleIngest} className="space-y-3">
          <div>
            <label className={labelClass}>Run ID</label>
            <input
              value={runId}
              onChange={(e) => setRunId(e.target.value)}
              placeholder="01JXYZ…"
              disabled={allMode}
              className={inputClass}
            />
          </div>
          <label className="flex items-center gap-2 text-xs text-[var(--color-text-muted)]">
            <input
              type="checkbox"
              checked={allMode}
              onChange={(e) => setAllMode(e.target.checked)}
              className="accent-[var(--color-accent)]"
            />
            Re-index all
          </label>
          <button
            type="submit"
            disabled={loading || (!allMode && !runId.trim())}
            className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white disabled:opacity-50 transition-colors"
          >
            {loading ? 'Ingesting…' : 'Ingest'}
          </button>
        </form>
        {msg && <p className="text-xs text-[var(--color-status-success)] font-mono">{msg}</p>}
        {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
        <CliRefRow cmd="nova ingest-capsule <run_id> | nova ingest-capsule --all" label="CLI reference" />
      </div>
    </Panel>
  );
}
