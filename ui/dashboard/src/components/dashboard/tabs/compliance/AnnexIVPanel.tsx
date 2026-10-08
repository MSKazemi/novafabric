import { useState, useCallback } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import { useLocalMru } from './useLocalMru';

// ---------- Annex IV export panel ----------

export default function AnnexIVPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [deploymentId, setDeploymentId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [deploymentSuggestions, pushDeploymentId] = useLocalMru('nova-deployment-ids');

  const run = useCallback(async () => {
    if (!runId.trim() || !deploymentId.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.exportAnnexIV(runId.trim(), deploymentId.trim());
      setResult(res);
      pushDeploymentId(deploymentId.trim());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, deploymentId, pushDeploymentId]);

  const downloadJson = useCallback(() => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `annex-iv-${deploymentId || 'export'}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result, deploymentId]);

  const complete = typeof result?.complete_elements === 'number' ? result.complete_elements as number : null;
  const total = typeof result?.total_elements === 'number' ? result.total_elements as number : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">EU AI Act Annex IV</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-002 · Regulation (EU) 2024/1689 · 15 mandatory technical documentation elements
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-002</span>
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
          value={deploymentId}
          onChange={setDeploymentId}
          suggestions={deploymentSuggestions}
          onEnter={run}
          placeholder="deployment_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      </div>

      <div className="flex gap-2">
        <div className="flex-1 font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
          $ nova export-annex-iv {runId || '<capsule_dir>'} --output-dir ./out --deployment-id {deploymentId || '<id>'}
        </div>
        <button
          onClick={run}
          disabled={loading || !runId.trim() || !deploymentId.trim()}
          className="px-3 py-1 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
        >
          {loading ? '…' : 'Export'}
        </button>
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
              {complete !== null && total !== null
                ? `${complete}/${total} elements complete`
                : 'Export ready'}
            </span>
            <button
              onClick={downloadJson}
              className="text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
            >
              ↓ Download JSON-LD
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
