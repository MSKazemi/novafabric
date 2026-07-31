// Eval regression comparison panel (nova eval compare). Extracted verbatim
// from GovernanceTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import CopyButton from '../../../ui/CopyButton';

interface MetricRow {
  name: string;
  baseline_value: number;
  candidate_value: number;
  delta: number;
  relative_delta: number;
  p_value: number | null;
  significant: boolean;
}

export default function EvalComparePanel() {
  const [baselineJson, setBaselineJson] = useState('');
  const [candidateJson, setCandidateJson] = useState('');
  const [alpha, setAlpha] = useState('0.05');
  const [minSamples, setMinSamples] = useState('5');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    suite_id?: string;
    regression_detected?: boolean;
    summary?: string;
    metrics?: MetricRow[];
  } | null>(null);

  const run = useCallback(async () => {
    if (!baselineJson.trim() || !candidateJson.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.evalCompare(
        baselineJson.trim(),
        candidateJson.trim(),
        parseFloat(alpha) || 0.05,
        parseInt(minSamples) || 5,
      );
      setResult(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [baselineJson, candidateJson, alpha, minSamples]);

  const cliCmd = 'nova eval compare baseline.json candidate.json';
  const textareaClass =
    'w-full text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-2 font-mono focus:border-[var(--color-accent)] focus:outline-none resize-y min-h-[80px]';
  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-20';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Eval Regression Comparison</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Compare two EvalResult JSON objects to detect metric regressions.
          </p>
        </div>
      </div>

      {/* JSON inputs */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Baseline EvalResult JSON
          </label>
          <textarea
            value={baselineJson}
            onChange={(e) => setBaselineJson(e.target.value)}
            placeholder={'{\n  "suite_id": "smoke-v1",\n  ...\n}'}
            className={textareaClass}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Candidate EvalResult JSON
          </label>
          <textarea
            value={candidateJson}
            onChange={(e) => setCandidateJson(e.target.value)}
            placeholder={'{\n  "suite_id": "smoke-v1",\n  ...\n}'}
            className={textareaClass}
          />
        </div>
      </div>

      {/* Alpha + min_samples */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Alpha</label>
          <input
            type="number"
            step="0.01"
            min="0.001"
            max="0.5"
            value={alpha}
            onChange={(e) => setAlpha(e.target.value)}
            className={inputClass}
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Min samples</label>
          <input
            type="number"
            min="1"
            max="1000"
            value={minSamples}
            onChange={(e) => setMinSamples(e.target.value)}
            className={inputClass}
          />
        </div>
      </div>

      <button
        onClick={run}
        disabled={loading || !baselineJson.trim() || !candidateJson.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !baselineJson.trim() || !candidateJson.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'comparing…' : 'Compare'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          {/* Regression banner */}
          <div className={clsx(
            'text-xs font-mono font-bold px-3 py-2 rounded border',
            result.regression_detected
              ? 'text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]'
              : result.ok
                ? 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
                : 'text-[var(--color-status-pending)] border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)]',
          )}>
            {result.regression_detected ? 'Regression detected' : result.ok ? 'No regression' : 'Comparison error'}
            {result.suite_id ? ` — suite: ${result.suite_id}` : ''}
          </div>

          {result.summary && (
            <p className="text-[10px] text-[var(--color-text-muted)] font-mono">{result.summary}</p>
          )}

          {/* Metrics table */}
          {result.metrics && result.metrics.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--color-border)]">
                    {['Metric', 'Baseline', 'Candidate', 'Delta', 'Rel%', 'Significant'].map((h) => (
                      <th key={h} className="text-left px-2 py-1 text-[var(--color-text-faint)] uppercase tracking-wider font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.metrics.map((m) => (
                    <tr key={m.name} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-bg-sunken)]">
                      <td className="px-2 py-1.5 text-[var(--color-text)]">{m.name}</td>
                      <td className="px-2 py-1.5 text-[var(--color-text-muted)]">{m.baseline_value.toFixed(4)}</td>
                      <td className="px-2 py-1.5 text-[var(--color-text-muted)]">{m.candidate_value.toFixed(4)}</td>
                      <td className={clsx(
                        'px-2 py-1.5',
                        m.delta < 0 ? 'text-[var(--color-status-failure)]' : m.delta > 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-text-faint)]',
                      )}>
                        {m.delta >= 0 ? '+' : ''}{m.delta.toFixed(4)}
                      </td>
                      <td className={clsx(
                        'px-2 py-1.5',
                        m.relative_delta < 0 ? 'text-[var(--color-status-failure)]' : m.relative_delta > 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-text-faint)]',
                      )}>
                        {isFinite(m.relative_delta) ? `${(m.relative_delta * 100).toFixed(1)}%` : '∞'}
                      </td>
                      <td className="px-2 py-1.5">
                        {m.significant
                          ? <span className="text-[var(--color-status-failure)] font-bold">yes</span>
                          : <span className="text-[var(--color-text-faint)]">no</span>
                        }
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* CLI equivalent */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[10px] font-mono text-[var(--color-text-muted)]">$ {cliCmd}</pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
      </div>
    </section>
  );
}
