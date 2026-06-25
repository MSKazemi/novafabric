/**
 * Evaluation suites — surfaces the `nova eval` command group:
 *   nova eval list     → registered suite adapters
 *   nova eval run      → run a suite against a captured run
 *   nova eval compare  → statistical baseline-vs-candidate regression check
 */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../../lib/api';
import { useMutation } from '../../../lib/useMutation';
import TabShell from './TabShell';
import DataTable, { type Column } from '../../ui/DataTable';
import ActionButton from '../../ui/ActionButton';
import EmptyState from '../../ui/EmptyState';

interface Suite {
  suite_id: string;
  version: string | null;
  oci_digest: string | null;
  entry_point: string;
  error?: string;
}

export default function EvalTab() {
  const [suites, setSuites] = useState<Suite[]>([]);
  const [suitesLoading, setSuitesLoading] = useState(true);
  const [suitesError, setSuitesError] = useState<string | null>(null);

  const [runId, setRunId] = useState('');
  const [suite, setSuite] = useState('');
  const [baseline, setBaseline] = useState('');
  const [candidate, setCandidate] = useState('');
  const [alpha, setAlpha] = useState('0.05');

  const loadSuites = () => {
    setSuitesLoading(true);
    setSuitesError(null);
    api.evalSuites()
      .then((r) => setSuites(r.suites ?? []))
      .catch((e) => setSuitesError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSuitesLoading(false));
  };
  useEffect(loadSuites, []);

  const runEval = useMutation(
    (rid: string, s: string) => api.evalRun(rid, s),
    { successMessage: (r) => (r.result.passed ? 'Eval passed' : 'Eval ran (failed thresholds)') },
  );
  const compare = useMutation(
    (b: string, c: string, a: number) => api.evalCompare(b, c, a),
    { successMessage: (r) => (r.regression_detected ? 'Regression detected' : 'No regression') },
  );

  const suiteColumns = useMemo<Column<Suite>[]>(() => [
    { key: 'suite_id', header: 'Suite', className: 'flex-1', sortValue: (r) => r.suite_id,
      render: (r) => <span className="font-mono">{r.suite_id}</span> },
    { key: 'version', header: 'Version', className: 'w-24', render: (r) => r.version ?? '—' },
    { key: 'oci_digest', header: 'OCI digest', className: 'w-40',
      render: (r) => <span className="font-mono text-[10px] text-[var(--color-text-faint)] truncate">{r.oci_digest ?? '—'}</span> },
    { key: 'entry_point', header: 'Entry point', className: 'flex-1',
      render: (r) => <span className="font-mono text-[10px] truncate">{r.entry_point}</span> },
  ], []);

  return (
    <TabShell
      title="Evaluation Suites"
      subtitle="Run standard eval suites and detect regressions against a baseline."
      cli={['nova eval list', 'nova eval run', 'nova eval compare']}
      help="Standard suites (GAIA, SWE-bench, AgentBench, MMLU, Smoke) run in OCI-pinned containers. Compare uses a paired statistical test to flag significant regressions."
      actions={<ActionButton onClick={loadSuites} pending={suitesLoading} variant="ghost" size="sm">Refresh</ActionButton>}
    >
      {/* Suites */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Registered suites</h3>
        <DataTable
          columns={suiteColumns}
          rows={suites}
          rowKey={(r) => r.suite_id}
          loading={suitesLoading}
          error={suitesError}
          onRetry={loadSuites}
          maxHeight={240}
          empty={<EmptyState message="No eval suites registered." cliCommand="pip install novafabric[eval]" />}
        />
      </section>

      {/* Run */}
      <section className="space-y-2 rounded border border-[var(--color-border)] p-4">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Run a suite</h3>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
            Run ID
            <input
              value={runId}
              onChange={(e) => setRunId(e.target.value)}
              placeholder="01JABC…"
              className="w-56 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
            />
          </label>
          <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
            Suite
            <select
              value={suite}
              onChange={(e) => setSuite(e.target.value)}
              className="w-44 px-2 py-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
            >
              <option value="">Select suite…</option>
              {suites.map((s) => <option key={s.suite_id} value={s.suite_id}>{s.suite_id}</option>)}
            </select>
          </label>
          <ActionButton
            onClick={() => runEval.run(runId.trim(), suite)}
            pending={runEval.pending}
            disabled={!runId.trim() || !suite}
            variant="primary"
          >
            Run eval
          </ActionButton>
        </div>
        {runEval.result && (
          <div className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-xs space-y-1">
            <div className="flex items-center gap-2">
              <span className={runEval.result.result.passed ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>
                {runEval.result.result.passed ? '✓ passed' : '✗ failed'}
              </span>
              <span className="font-mono text-[var(--color-text-faint)]">{runEval.result.result.suite_id}</span>
            </div>
            {runEval.result.result.metrics.map((m) => (
              <div key={m.name} className="flex items-center gap-2 font-mono text-[10px]">
                <span className="w-40 truncate">{m.name}</span>
                <span className={m.passed ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>
                  {m.value} {m.unit} (≥ {m.threshold})
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Compare */}
      <section className="space-y-2 rounded border border-[var(--color-border)] p-4">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Compare (regression check)</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
            Baseline results JSON
            <textarea
              value={baseline}
              onChange={(e) => setBaseline(e.target.value)}
              rows={4}
              placeholder='{"metrics": [...]}'
              className="px-2 py-1 text-[10px] font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
            />
          </label>
          <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
            Candidate results JSON
            <textarea
              value={candidate}
              onChange={(e) => setCandidate(e.target.value)}
              rows={4}
              placeholder='{"metrics": [...]}'
              className="px-2 py-1 text-[10px] font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
            />
          </label>
        </div>
        <div className="flex items-end gap-2">
          <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
            Alpha
            <input
              value={alpha}
              onChange={(e) => setAlpha(e.target.value)}
              className="w-20 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
            />
          </label>
          <ActionButton
            onClick={() => compare.run(baseline, candidate, Number(alpha) || 0.05)}
            pending={compare.pending}
            disabled={!baseline.trim() || !candidate.trim()}
            variant="primary"
          >
            Compare
          </ActionButton>
        </div>
        {compare.result?.metrics && (
          <div className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-[10px] font-mono space-y-1">
            <div className={compare.result.regression_detected ? 'text-[var(--color-status-failure)]' : 'text-[var(--color-status-success)]'}>
              {compare.result.summary ?? (compare.result.regression_detected ? 'Regression detected' : 'No regression')}
            </div>
            {compare.result.metrics.map((m) => (
              <div key={m.name} className="flex items-center gap-2">
                <span className="w-36 truncate">{m.name}</span>
                <span>{m.baseline_value} → {m.candidate_value}</span>
                <span className={m.significant ? 'text-[var(--color-status-failure)]' : 'text-[var(--color-text-faint)]'}>
                  Δ{m.delta.toFixed(3)} {m.p_value != null ? `(p=${m.p_value.toFixed(3)})` : ''}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>
    </TabShell>
  );
}
