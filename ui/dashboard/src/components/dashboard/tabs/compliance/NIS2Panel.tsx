import { useState, useCallback } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import { useLocalMru } from './useLocalMru';

// ---------- NIS2 export panel ----------

export default function NIS2Panel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [incidentId, setIncidentId] = useState('');
  const [phase, setPhase] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [incidentSuggestions, pushIncidentId] = useLocalMru('nova-incident-ids');

  const run = useCallback(async () => {
    if (!runId.trim() || !incidentId.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.exportNis2(runId.trim(), incidentId.trim(), phase);
      setResult(res);
      pushIncidentId(incidentId.trim());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, incidentId, phase, pushIncidentId]);

  const downloadJson = useCallback(() => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `nis2-phase${phase}-${incidentId || 'report'}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result, phase, incidentId]);

  const missingFields = typeof result?.missing_fields === 'number' ? result.missing_fields as number : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">NIS2 Incident Report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-005 · Directive (EU) 2022/2555 Art. 23 · Phase 1 ≤24h, Phase 2 ≤72h, Phase 3 ≤1 month
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-005</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={run}
          placeholder="capsule run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <SuggestInput
          value={incidentId}
          onChange={setIncidentId}
          suggestions={incidentSuggestions}
          onEnter={run}
          placeholder="incident_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      </div>

      <div className="flex items-center gap-2">
        <label className="text-[10px] text-[var(--color-text-faint)] shrink-0">Phase:</label>
        {([1, 2, 3] as const).map((p) => (
          <button
            key={p}
            onClick={() => setPhase(p)}
            className={[
              'text-[10px] px-2.5 py-1 rounded border transition-colors',
              phase === p
                ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_15%,transparent)] text-[var(--color-accent)]'
                : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)]',
            ].join(' ')}
          >
            {p}
          </button>
        ))}
        <span className="text-[10px] text-[var(--color-text-faint)]">
          {phase === 1 ? '≤24h initial' : phase === 2 ? '≤72h detailed' : '≤1 month final'}
        </span>
        <div className="flex-1" />
        <button
          onClick={run}
          disabled={loading || !runId.trim() || !incidentId.trim()}
          className="px-3 py-1 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? '…' : 'Export'}
        </button>
      </div>

      <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
        $ nova export-nis2 {runId || '<capsule_dir>'} --output ./out/nis2.json --incident-id {incidentId || '<id>'} --phase {phase}
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
              Phase {phase} report generated
              {missingFields !== null && missingFields > 0 && (
                <span className="text-[var(--color-status-pending)] ml-2">· {missingFields} fields missing (cap-006 required)</span>
              )}
            </span>
            <button
              onClick={downloadJson}
              className="text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
            >
              ↓ Download JSON
            </button>
          </div>
          <pre className="text-[9px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2 max-h-48 overflow-auto whitespace-pre-wrap text-[var(--color-text-faint)]">
            {JSON.stringify(result, null, 2).slice(0, 2000)}
            {JSON.stringify(result, null, 2).length > 2000 && '\n… (truncated)'}
          </pre>
        </div>
      )}
    </section>
  );
}
