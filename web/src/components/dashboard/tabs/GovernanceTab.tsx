import { useState, useCallback, useEffect } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import { SuggestInput } from '../../ui/SuggestInput';
import CopyButton from '../../ui/CopyButton';

// ── Eval Compare panel ────────────────────────────────────────────────────────

interface MetricRow {
  name: string;
  baseline_value: number;
  candidate_value: number;
  delta: number;
  relative_delta: number;
  p_value: number | null;
  significant: boolean;
}

function EvalComparePanel() {
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

// ── Risk tier colour coding ───────────────────────────────────────────────────

const EU_TIER_CONFIG: Record<string, { label: string; colorClass: string }> = {
  prohibited:   { label: 'Prohibited',    colorClass: 'text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]' },
  high_risk:    { label: 'High Risk',     colorClass: 'text-[var(--color-status-pending)] border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]' },
  limited_risk: { label: 'Limited Risk',  colorClass: 'text-[var(--color-accent)] border-[color-mix(in_oklab,var(--color-accent)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]' },
  minimal_risk: { label: 'Minimal Risk',  colorClass: 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]' },
};

const NIST_IMPACT_CONFIG: Record<string, { label: string; colorClass: string }> = {
  critical: { label: 'Critical', colorClass: 'text-[var(--color-status-failure)]' },
  high:     { label: 'High',     colorClass: 'text-[var(--color-status-pending)]' },
  medium:   { label: 'Medium',   colorClass: 'text-[var(--color-accent)]' },
  low:      { label: 'Low',      colorClass: 'text-[var(--color-status-success)]' },
};

const VOCAB_OPTIONS = [
  'eu-ai-act/2024.1.0',
  'nist-ai-rmf/1.0.0',
  'omb-m-24-10/1.0.0',
];

// ── Classification result display ─────────────────────────────────────────────

interface ClassifyResult {
  eu_ai_act_tier: string;
  eu_ai_act_role: string;
  eu_ai_act_annex_iii_items: Array<{ item_id: string; description: string; citation: string }>;
  nist_rmf_impact: string;
  omb_flags: string[];
  rationale: string;
  citations: string[];
  vocabulary_version: string;
}

function ClassifyResultPanel({
  result,
  runId,
  vocabulary,
}: {
  result: ClassifyResult;
  runId: string;
  vocabulary: string;
}) {
  const euConfig = EU_TIER_CONFIG[result.eu_ai_act_tier] ?? { label: result.eu_ai_act_tier, colorClass: 'text-[var(--color-text-muted)]' };
  const nistConfig = NIST_IMPACT_CONFIG[result.nist_rmf_impact] ?? { label: result.nist_rmf_impact, colorClass: 'text-[var(--color-text-muted)]' };
  const cliCmd = `nova classify from-capsule <capsule_dir> --vocabulary ${vocabulary}`;

  return (
    <div className="space-y-3">
      {/* Primary tier badges */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className={clsx('text-xs font-mono font-bold px-3 py-1.5 rounded border', euConfig.colorClass)}>
          EU AI Act: {euConfig.label}
        </div>
        <div className={clsx('text-xs font-mono font-bold px-2 py-1 rounded', nistConfig.colorClass)}>
          NIST RMF: {nistConfig.label} impact
        </div>
        {result.omb_flags.length > 0 && (
          <div className="text-xs font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)]">
            OMB: {result.omb_flags.join(', ')}
          </div>
        )}
        {result.omb_flags.length === 0 && (
          <div className="text-xs font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
            OMB: no flags
          </div>
        )}
      </div>

      {/* Role */}
      <div className="flex items-center gap-2 text-[10px] text-[var(--color-text-faint)] font-mono">
        <span>role: {result.eu_ai_act_role}</span>
        <span className="opacity-40">·</span>
        <span className="truncate">vocabularies: {result.vocabulary_version}</span>
      </div>

      {/* Rationale */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Rationale</p>
        <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2.5 whitespace-pre-wrap text-[var(--color-text-muted)] max-h-40 overflow-auto">
          {result.rationale}
        </pre>
      </div>

      {/* Matched Annex III items */}
      {result.eu_ai_act_annex_iii_items.length > 0 && (
        <div className="space-y-1">
          <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Matched Annex III items
          </p>
          <ul className="space-y-1">
            {result.eu_ai_act_annex_iii_items.map((item) => (
              <li
                key={item.item_id}
                className="text-[10px] font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 space-y-0.5"
              >
                <span className="text-[var(--color-text)]">[{item.item_id}] {item.description}</span>
                <span className="block text-[var(--color-text-faint)]">{item.citation}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* CLI equivalent */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[10px] font-mono text-[var(--color-text-muted)]">{cliCmd}</pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
        <p className="text-[9px] text-[var(--color-text-faint)]">
          Note: dashboard classification uses minimal capsule metadata. Run the CLI with a full system description file for detailed results.
        </p>
      </div>
    </div>
  );
}

// ── Risk Classification panel ─────────────────────────────────────────────────

function ClassifyPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [vocabulary, setVocabulary] = useState('eu-ai-act/2024.1.0');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ClassifyResult | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.classifyFromCapsule(id, vocabulary);
      setResult(res.result as unknown as ClassifyResult);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, vocabulary]);

  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Risk Classification</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            ADR-0056 · EU AI Act 2024/1689 · NIST AI RMF 600-1 · OMB M-24-10
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
          ADR-0056
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule run ID
          </label>
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_2024_..."
            className={inputClass + ' w-full'}
            onEnter={run}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Vocabulary
          </label>
          <select
            value={vocabulary}
            onChange={(e) => setVocabulary(e.target.value)}
            className={inputClass + ' w-full'}
          >
            {VOCAB_OPTIONS.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
      </div>

      <button
        onClick={run}
        disabled={loading || !runId.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !runId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'classifying…' : 'Classify'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <ClassifyResultPanel result={result} runId={runId.trim()} vocabulary={vocabulary} />
      )}
    </section>
  );
}

// ── EuAiActStatusPanel ────────────────────────────────────────────────────────

function EuAiActStatusPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    high_risk: boolean;
    provider_mode: boolean;
    retention_months: number;
    deadline: string;
    note: string;
  } | null>(null);

  useEffect(() => {
    setLoading(true);
    api.euaiactStatus()
      .then(r => { setResult(r); setError(null); })
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">EU AI Act Art.12 Status</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Transparency logging status — mirrors <code className="font-mono">nova euaiact status</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0076
        </span>
      </div>

      {loading && (
        <p className="text-xs text-[var(--color-text-faint)]">Loading…</p>
      )}

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
            <span className="text-[var(--color-text-faint)]">High-Risk Mode</span>
            <span>
              {result.high_risk
                ? <span className="text-[var(--color-status-success)] font-medium">active</span>
                : <span className="text-[var(--color-text-faint)]">inactive</span>}
            </span>

            <span className="text-[var(--color-text-faint)]">Role</span>
            <span className="text-[var(--color-text)] font-mono">
              {result.provider_mode ? 'provider' : 'deployer'}
            </span>

            <span className="text-[var(--color-text-faint)]">Retention floor</span>
            <span className="text-[var(--color-text)]">
              {result.retention_months} months (Art.18 provider / Art.12 deployer)
            </span>

            <span className="text-[var(--color-text-faint)]">Art.50 deadline</span>
            <span className="text-[var(--color-text)] font-mono">2026-08-02</span>
          </div>

          {result.note && (
            <p className="text-[10px] text-[var(--color-text-faint)] border-t border-[var(--color-border)] pt-2">
              {result.note}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

// ── EuAiActExportPanel ────────────────────────────────────────────────────────

function EuAiActExportPanel() {
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    records: Array<{ run_id: string; created_at: string; status: string; logging_event_type: string[] }>;
    count: number;
    retention_months: number;
    mode: string;
  } | null>(null);

  function handleExport() {
    setLoading(true);
    setError(null);
    setResult(null);
    api.euaiactExport(fromDate || undefined, toDate || undefined)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">EU AI Act Art.12 Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export transparency logging records — mirrors <code className="font-mono">nova euaiact export</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0076
        </span>
      </div>

      <div className="flex flex-wrap gap-3 items-end">
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[var(--color-text-faint)]">From (ISO 8601, optional)</label>
          <input
            type="text"
            placeholder="2026-01-01T00:00:00Z"
            value={fromDate}
            onChange={e => setFromDate(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[var(--color-text-faint)]">To (ISO 8601, optional)</label>
          <input
            type="text"
            placeholder="2026-12-31T23:59:59Z"
            value={toDate}
            onChange={e => setToDate(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? 'Exporting…' : 'Export'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              {result.count} record{result.count !== 1 ? 's' : ''} · mode: <span className="font-mono">{result.mode}</span>
            </span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>

          {result.records.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--color-border)]">
                    <th className="text-left py-1 pr-3 text-[var(--color-text-faint)] font-medium">run_id</th>
                    <th className="text-left py-1 pr-3 text-[var(--color-text-faint)] font-medium">created_at</th>
                    <th className="text-left py-1 pr-3 text-[var(--color-text-faint)] font-medium">status</th>
                    <th className="text-left py-1 text-[var(--color-text-faint)] font-medium">event types</th>
                  </tr>
                </thead>
                <tbody>
                  {result.records.map((rec, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)] border-opacity-50 hover:bg-[var(--color-bg-sunken)]">
                      <td className="py-1 pr-3 text-[var(--color-text)]">{rec.run_id.slice(0, 24)}</td>
                      <td className="py-1 pr-3 text-[var(--color-text-faint)]">{rec.created_at}</td>
                      <td className="py-1 pr-3 text-[var(--color-text)]">{rec.status}</td>
                      <td className="py-1 text-[var(--color-text-faint)]">{rec.logging_event_type.join(', ')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-[10px] text-[var(--color-text-faint)]">
            Retention floor: {result.retention_months} months
          </p>
        </div>
      )}
    </section>
  );
}

// ── VocabulariesPanel ─────────────────────────────────────────────────────────

interface VocabularyRow {
  framework: string;
  version: string;
  reference: string;
  path: string;
}

function VocabulariesPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; vocabularies: VocabularyRow[] } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.governanceVocabularies();
      setResult(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Regulatory Vocabularies</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Available EU AI Act / NIST AI RMF vocabulary versions — <code className="font-mono">nova classify list-vocabularies</code>
          </p>
        </div>
      </div>

      <button
        onClick={load}
        disabled={loading}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'loading…' : 'Load'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && result.vocabularies.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[10px] font-mono border-collapse">
            <thead>
              <tr className="border-b border-[var(--color-border)]">
                {['Framework', 'Version', 'Reference', 'Path'].map((h) => (
                  <th key={h} className="text-left px-2 py-1 text-[var(--color-text-faint)] uppercase tracking-wider font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.vocabularies.map((v) => (
                <tr key={`${v.framework}/${v.version}`} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-bg-sunken)]">
                  <td className="px-2 py-1.5 text-[var(--color-text)]">{v.framework}</td>
                  <td className="px-2 py-1.5 text-[var(--color-text-muted)]">{v.version}</td>
                  <td className="px-2 py-1.5 text-[var(--color-text-muted)]">{v.reference}</td>
                  <td className="px-2 py-1.5 text-[var(--color-text-faint)]">{v.path}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result && result.vocabularies.length === 0 && (
        <p className="text-xs text-[var(--color-text-faint)]">No vocabularies found.</p>
      )}
    </section>
  );
}

// ── ManualClassifyPanel ───────────────────────────────────────────────────────

function ManualClassifyPanel() {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [useCaseDomain, setUseCaseDomain] = useState('');
  const [deploymentContext, setDeploymentContext] = useState('');
  const [usesBiometrics, setUsesBiometrics] = useState(false);
  const [affectsFundamentalRights, setAffectsFundamentalRights] = useState(false);
  const [isGeneralPurpose, setIsGeneralPurpose] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const run = useCallback(async () => {
    if (!name.trim() || !description.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.classifyManual({
        name: name.trim(),
        description: description.trim(),
        use_case_domain: useCaseDomain.trim(),
        deployment_context: deploymentContext.trim(),
        uses_biometrics: usesBiometrics,
        affects_fundamental_rights: affectsFundamentalRights,
        is_general_purpose: isGeneralPurpose,
      });
      setResult(res.classification);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [name, description, useCaseDomain, deploymentContext, usesBiometrics, affectsFundamentalRights, isGeneralPurpose]);

  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';
  const textareaClass =
    'w-full text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-2 font-mono focus:border-[var(--color-accent)] focus:outline-none resize-y min-h-[80px]';

  const euTier = typeof result?.eu_ai_act_tier === 'string' ? result.eu_ai_act_tier : null;
  const nistLevel = typeof result?.nist_rmf_level === 'string' ? result.nist_rmf_level : null;
  const ombTier = typeof result?.omb_tier === 'string' ? result.omb_tier : null;
  const rationale = typeof result?.rationale === 'string' ? result.rationale : null;
  const euConfig = euTier ? (EU_TIER_CONFIG[euTier] ?? { label: euTier, colorClass: 'text-[var(--color-text-muted)]' }) : null;
  const nistConfig = nistLevel ? (NIST_IMPACT_CONFIG[nistLevel] ?? { label: nistLevel, colorClass: 'text-[var(--color-text-muted)]' }) : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Manual Risk Classification</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Classify an AI system from a manual description — <code className="font-mono">nova classify run</code>
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Name
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-ai-system"
            className={inputClass + ' w-full'}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Use case domain
          </label>
          <input
            type="text"
            value={useCaseDomain}
            onChange={(e) => setUseCaseDomain(e.target.value)}
            placeholder="finance, healthcare, general"
            className={inputClass + ' w-full'}
          />
        </div>
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Description
        </label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What the system does, who uses it, and how decisions affect people."
          className={textareaClass}
        />
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Deployment context
        </label>
        <input
          type="text"
          value={deploymentContext}
          onChange={(e) => setDeploymentContext(e.target.value)}
          placeholder="credit_scoring"
          className={inputClass + ' w-full'}
        />
      </div>

      <div className="flex items-center gap-4 flex-wrap">
        <label className="flex items-center gap-1.5 text-[10px] font-mono text-[var(--color-text-muted)] cursor-pointer">
          <input
            type="checkbox"
            checked={usesBiometrics}
            onChange={(e) => setUsesBiometrics(e.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          uses biometrics
        </label>
        <label className="flex items-center gap-1.5 text-[10px] font-mono text-[var(--color-text-muted)] cursor-pointer">
          <input
            type="checkbox"
            checked={affectsFundamentalRights}
            onChange={(e) => setAffectsFundamentalRights(e.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          affects fundamental rights
        </label>
        <label className="flex items-center gap-1.5 text-[10px] font-mono text-[var(--color-text-muted)] cursor-pointer">
          <input
            type="checkbox"
            checked={isGeneralPurpose}
            onChange={(e) => setIsGeneralPurpose(e.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          general purpose
        </label>
      </div>

      <button
        onClick={run}
        disabled={loading || !name.trim() || !description.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !name.trim() || !description.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'classifying…' : 'Classify'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          {/* Primary tier badges */}
          <div className="flex items-center gap-3 flex-wrap">
            {euConfig && (
              <div className={clsx('text-xs font-mono font-bold px-3 py-1.5 rounded border', euConfig.colorClass)}>
                EU AI Act: {euConfig.label}
              </div>
            )}
            {nistConfig && (
              <div className={clsx('text-xs font-mono font-bold px-2 py-1 rounded', nistConfig.colorClass)}>
                NIST RMF: {nistConfig.label}
              </div>
            )}
            {ombTier && (
              <div className="text-xs font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)]">
                OMB: {ombTier}
              </div>
            )}
          </div>

          {rationale && (
            <div className="space-y-1">
              <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Rationale</p>
              <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2.5 whitespace-pre-wrap text-[var(--color-text-muted)] max-h-40 overflow-auto">
                {rationale}
              </pre>
            </div>
          )}

          {/* Full JSON */}
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">Full classification JSON</span>
            <CopyButton text={JSON.stringify(result, null, 2)} label="JSON" />
          </div>
        </div>
      )}
    </section>
  );
}

// ── GovernanceTab ─────────────────────────────────────────────────────────────

export default function GovernanceTab() {
  const [runIds, setRunIds] = useState<string[]>([]);

  useEffect(() => {
    api.listRuns()
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-[var(--color-text)]">Governance</h2>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            AI system risk-tier classification — EU AI Act · NIST AI RMF · OMB M-24-10 · Eval regression comparison
          </p>
        </div>
        <div className="flex gap-1.5">
          {(['ADR-0056'] as const).map(label => (
            <span
              key={label}
              className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      <ClassifyPanel runIds={runIds} />

      <VocabulariesPanel />

      <ManualClassifyPanel />

      <EvalComparePanel />

      <EuAiActStatusPanel />

      <EuAiActExportPanel />

      {/* About section */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-2">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          About risk classification
        </p>
        <p className="text-xs text-[var(--color-text-muted)]">
          NovaFabric classifies AI systems against three regulatory frameworks using a deterministic,
          rules-based pipeline (ADR-0056):
        </p>
        <ul className="text-xs text-[var(--color-text-muted)] space-y-1 pl-3 list-disc">
          <li>
            <span className="font-semibold text-[var(--color-text)]">EU AI Act (Regulation EU 2024/1689)</span>
            {' '}— four tiers: Prohibited, High-Risk (Annex III), Limited, Minimal.
          </li>
          <li>
            <span className="font-semibold text-[var(--color-text)]">NIST AI RMF 600-1</span>
            {' '}— four impact levels: Low, Medium, High, Critical.
          </li>
          <li>
            <span className="font-semibold text-[var(--color-text)]">OMB M-24-10</span>
            {' '}— rights-impacting and safety-impacting flags for US federal agencies.
          </li>
        </ul>
        <p className="text-[10px] text-[var(--color-text-faint)]">
          The dashboard panel classifies using metadata inferred from the capsule. For precise results,
          use <code className="font-mono">nova classify from-capsule</code> with a full system description.
        </p>
      </div>
    </div>
  );
}
