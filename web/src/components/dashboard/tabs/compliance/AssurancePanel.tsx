import { useState, useCallback } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';

// ---------- OWASP LLM Assurance panel (nova assure — E-10) ----------

export default function AssurancePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [capsulePath, setCapsulePath] = useState('');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.assureRun>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.assureRun(id, capsulePath.trim() || undefined);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, capsulePath]);

  const statusCls = (s: string) => {
    switch (s) {
      case 'PASS': return 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]';
      case 'FAIL': return 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]';
      case 'WARN': return 'text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_10%,transparent)]';
      default: return 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]';
    }
  };

  const statusIcon = (s: string) => {
    switch (s) {
      case 'PASS': return '✓';
      case 'FAIL': return '✗';
      case 'WARN': return '!';
      default: return '~';
    }
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">OWASP LLM Assurance Check</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Run 10 OWASP Top 10 for LLM (2025) evidence checks — <code className="font-mono">nova assure</code>
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">E-10</span>
      </div>
      <div className="space-y-1.5">
        <SuggestInput
          value={runId}
          onChange={setRunId}
          suggestions={runIds}
          onEnter={run}
          placeholder="Run ID (e.g. 01ABC123…)"
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <input
          value={capsulePath}
          onChange={e => setCapsulePath(e.target.value)}
          placeholder="Capsule path (optional — auto-resolved from run ID if blank)"
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      </div>
      <button
        onClick={run}
        disabled={loading || !runId.trim()}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Run Assurance Checks'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && result.ok === false && (
        <p className="text-xs text-[var(--color-text-faint)] font-mono">{result.reason ?? 'Capsule not found'}</p>
      )}
      {result && result.overall_status && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className={clsx('text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded', statusCls(result.overall_status))}>
              {result.overall_status}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.pass_count}P / {result.warn_count}W / {result.fail_count}F / {result.skip_count}S
            </span>
          </div>
          <div className="space-y-0.5 max-h-52 overflow-y-auto">
            {result.results?.map(r => (
              <div key={r.check_id} className="flex items-start gap-1.5 text-[10px]">
                <span className={clsx('font-mono shrink-0 w-4', statusCls(r.status).split(' ')[0])}>
                  {statusIcon(r.status)}
                </span>
                <span className="font-mono text-[var(--color-text-faint)] shrink-0 w-16">{r.check_id}</span>
                <span className="text-[var(--color-text)] leading-tight">{r.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
