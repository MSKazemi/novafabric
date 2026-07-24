/**
 * CostToolsPanel — the P6 cost-analytics trio read surface (ADR-0201).
 *
 * A document-driven panel (same shape as the P4 query panel): pick one of the
 * three pure ``nova cost`` cores, edit its JSON input, and see the computed
 * report. Each tool posts to its own endpoint and renders the descriptive
 * result — no cost verdict, no quota, just the composition/attribution the
 * core returns. The inputs are the exact documents the CLI commands accept.
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import {
  api,
  type SpendAttribution,
  type FairnessReport,
  type UsageBreakdown,
} from '../../lib/api';

type Tool = 'attribute' | 'fairness' | 'usage-breakdown';

const TOOLS: Array<{ id: Tool; label: string; cli: string; sample: string }> = [
  {
    id: 'attribute',
    label: 'Attribution',
    cli: 'nova cost attribute',
    sample: JSON.stringify(
      {
        runs: [
          { run_id: 'r1', status: 'success', cost: 3.0 },
          { run_id: 'r2', status: 'failed', cost: 1.0 },
        ],
        productive_statuses: ['success'],
      },
      null,
      2,
    ),
  },
  {
    id: 'fairness',
    label: 'Fairness',
    cli: 'nova cost fairness',
    sample: JSON.stringify(
      { totals: { cost: { 'agent-a': 3.0, 'agent-b': 1.0 } } },
      null,
      2,
    ),
  },
  {
    id: 'usage-breakdown',
    label: 'Usage breakdown',
    cli: 'nova cost usage-breakdown',
    sample: JSON.stringify(
      { usage_totals: { input_tokens: 600, output_tokens: 400, cached_tokens: 120 } },
      null,
      2,
    ),
  },
];

const inputClass =
  'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

type Result =
  | { tool: 'attribute'; data: SpendAttribution }
  | { tool: 'fairness'; data: FairnessReport }
  | { tool: 'usage-breakdown'; data: UsageBreakdown };

function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

function ResultView({ result }: { result: Result }) {
  if (result.tool === 'attribute') {
    const d = result.data;
    return (
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs font-mono">
        <dt className="text-[var(--color-text-muted)]">total spend</dt>
        <dd className="text-[var(--color-text)] text-right">{d.total_spend}</dd>
        <dt className="text-[var(--color-text-muted)]">productive</dt>
        <dd className="text-[var(--color-status-success)] text-right">{d.productive_spend}</dd>
        <dt className="text-[var(--color-text-muted)]">wasted</dt>
        <dd className="text-[var(--color-status-failure)] text-right">{d.wasted_spend}</dd>
        <dt className="text-[var(--color-text-muted)]">wasted fraction</dt>
        <dd className="text-[var(--color-text)] text-right">{pct(d.wasted_fraction)}</dd>
      </dl>
    );
  }
  if (result.tool === 'fairness') {
    return (
      <div className="space-y-2">
        {result.data.metrics.map((m) => (
          <div key={m.dimension}>
            <p className="text-[11px] font-mono text-[var(--color-text)]">
              {m.dimension}: gini={m.gini.toFixed(3)} · max/mean={m.max_mean_ratio.toFixed(2)}
            </p>
            <ul className="text-[11px] font-mono text-[var(--color-text-muted)] pl-3">
              {Object.entries(m.shares).map(([agent, share]) => (
                <li key={agent}>
                  {agent}: {pct(share)}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    );
  }
  const d = result.data;
  return (
    <div className="space-y-1 text-[11px] font-mono text-[var(--color-text-muted)]">
      <p className="text-[var(--color-text)]">{d.counted_tokens} counted token(s)</p>
      {Object.entries(d.composition).map(([t, share]) => (
        <p key={t} className="pl-3">
          {t}: {pct(share)}
        </p>
      ))}
      {d.cached_read_ratio !== null && <p className="pl-3">cached-read: {pct(d.cached_read_ratio)}</p>}
      <p className="pl-3">
        reasoning: {String(d.has_reasoning_tokens)} · multimodal: {String(d.is_multimodal)}
      </p>
    </div>
  );
}

export default function CostToolsPanel() {
  const [tool, setTool] = useState<Tool>('attribute');
  const [doc, setDoc] = useState<string>(TOOLS[0].sample);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const active = TOOLS.find((t) => t.id === tool)!;

  const selectTool = (id: Tool) => {
    setTool(id);
    setDoc(TOOLS.find((t) => t.id === id)!.sample);
    setResult(null);
    setErr(null);
  };

  const run = async () => {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const body = JSON.parse(doc) as Record<string, unknown>;
      if (tool === 'attribute') {
        const data = await api.costAttribute(body as { runs: unknown[]; productive_statuses?: string[] });
        setResult({ tool, data });
      } else if (tool === 'fairness') {
        const data = await api.costFairness(body as { totals: Record<string, Record<string, number>> });
        setResult({ tool, data });
      } else {
        const data = await api.costUsageBreakdown(body as { usage_totals: Record<string, unknown> });
        setResult({ tool, data });
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="inline-flex rounded-md border border-[var(--color-border)] overflow-hidden text-xs">
        {TOOLS.map((t) => (
          <button
            key={t.id}
            onClick={() => selectTool(t.id)}
            className={clsx(
              'px-3 py-1 font-medium transition-colors border-l first:border-l-0 border-[var(--color-border)]',
              tool === t.id
                ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]',
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
        CLI parity: <code>{active.cli}</code> · descriptive analytics only (no cost verdict)
      </p>

      <textarea
        value={doc}
        onChange={(e) => setDoc(e.target.value)}
        spellCheck={false}
        rows={8}
        className={clsx(inputClass, 'w-full resize-y')}
      />

      <button
        onClick={run}
        disabled={busy}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          busy
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {busy ? 'computing…' : 'Compute'}
      </button>

      {err && (
        <div className="text-xs text-[var(--color-status-failure)] font-mono break-all">{err}</div>
      )}
      {result && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3">
          <ResultView result={result} />
        </div>
      )}
    </div>
  );
}
