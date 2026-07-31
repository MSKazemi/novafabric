// KG agent query (nova kg query). Extracted verbatim from KGTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { inputClass, labelClass } from './shared';

export default function KGQueryPanel() {
  const [agentId, setAgentId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean; agent_id?: string; models?: string[]; tools?: string[]; error?: string;
  } | null>(null);

  const handleQuery = useCallback(async () => {
    if (!agentId.trim()) return;
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgQuery(agentId.trim());
      setResult(r);
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">KG Agent Query</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg query &lt;agent_id&gt; — list models and tools observed for an agent
        </p>
      </div>
      <div className="space-y-1">
        <label className={labelClass}>Agent ID</label>
        <input
          type="text"
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleQuery()}
          placeholder="agent-001 or run_id"
          className={`w-full ${inputClass}`}
        />
      </div>
      <button
        onClick={handleQuery}
        disabled={loading || !agentId.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !agentId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'querying…' : 'Query'}
      </button>
      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono space-y-1',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)]',
        )}>
          {result.ok ? (
            <>
              <div className="text-[var(--color-status-success)]">Agent: {result.agent_id}</div>
              <div>Models: {result.models?.join(', ') || '(none)'}</div>
              <div>Tools: {result.tools?.join(', ') || '(none)'}</div>
            </>
          ) : (
            <span className="text-[var(--color-status-failure)]">✗ {result.error}</span>
          )}
        </div>
      )}
    </section>
  );
}
