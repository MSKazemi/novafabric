import { useCallback, useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import { SuggestInput } from '../../ui/SuggestInput';
import ReportChart from '../ReportChart';
import CostToolsPanel from '../CostToolsPanel';

// DB-COST-1 — Cost report tab (v0.19.0, cap-002 / ADR-0066)
// Stub-aware: degrades gracefully when NOVA_CLICKHOUSE_URL is unset.

interface PricingRow {
  model: string;
  input_price_per_1k_usd: number;
  output_price_per_1k_usd: number;
}

interface CostReport {
  ok: boolean;
  backend: string;
  reason?: string;
  clickhouse_url?: string;
  run_id: string | null;
  days: number;
  totals: { input_tokens: number; completion_tokens: number; cost_usd: number };
  by_model: Array<Record<string, unknown>>;
  unpriced_models?: string[];
  note: string;
}

const inputClass =
  'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

const labelClass =
  'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

function PricingPanel({ rows }: { rows: PricingRow[] }) {
  if (!rows.length) {
    return (
      <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
        no pricing entries
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px] font-mono">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
            <th className="text-left px-3 py-1.5">model</th>
            <th className="text-right px-3 py-1.5">input / 1k tokens</th>
            <th className="text-right px-3 py-1.5">output / 1k tokens</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.model} className="border-b border-[var(--color-border)] last:border-b-0">
              <td className="px-3 py-1.5 text-[var(--color-text)]">{r.model}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">${r.input_price_per_1k_usd.toFixed(5)}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">${r.output_price_per_1k_usd.toFixed(5)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReportPanel({ report }: { report: CostReport }) {
  const backendColor = report.ok
    ? 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)]'
    : 'text-[var(--color-status-pending)] border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)]';
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={clsx('text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border', backendColor)}>
          backend: {report.backend}
        </span>
        {report.clickhouse_url && (
          <span className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] truncate max-w-md">
            {report.clickhouse_url}
          </span>
        )}
        {report.reason && (
          <span className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
            {report.reason}
          </span>
        )}
      </div>

      <div className="grid grid-cols-3 gap-3">
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <p className={labelClass}>input tokens</p>
          <p className="text-lg font-mono font-bold text-[var(--color-text)]">{report.totals.input_tokens.toLocaleString()}</p>
        </div>
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <p className={labelClass}>completion tokens</p>
          <p className="text-lg font-mono font-bold text-[var(--color-text)]">{report.totals.completion_tokens.toLocaleString()}</p>
        </div>
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <p className={labelClass}>cost (USD)</p>
          <p className="text-lg font-mono font-bold text-[var(--color-text)]">${report.totals.cost_usd.toFixed(4)}</p>
        </div>
      </div>

      {/* A model with no published price contributes $0.00 to the total. That is
          not a measurement that the calls were free, so the figure must not be
          shown bare — the tokens above are real either way. */}
      {report.unpriced_models && report.unpriced_models.length > 0 && (
        <div className="rounded border border-[var(--color-warning,#b45309)] bg-[var(--color-bg-sunken)] px-3 py-2 text-xs">
          <span className="font-mono uppercase tracking-wider text-[var(--color-warning,#b45309)]">
            no published price
          </span>
          <span className="ml-2 text-[var(--color-text-muted)]">
            {report.unpriced_models.join(', ')} — token counts above are exact; the
            cost excludes {report.unpriced_models.length === 1 ? 'this model' : 'these models'}{' '}
            because no price is known, not because the calls were free.
          </span>
        </div>
      )}

      {report.by_model.length > 0 && (() => {
        // Chart only when the backend really returned per-model rows — key
        // names differ per backend, so detect them instead of assuming.
        const sample = report.by_model[0];
        const x = ['model', 'model_name', 'name'].find(k => k in sample);
        const y = ['cost_usd', 'total_cost_usd', 'cost', 'input_tokens'].find(
          k => k in sample,
        );
        if (!x || !y) return null;
        return (
          <ReportChart
            spec={{
              kind: 'bar',
              x,
              y: [y],
              colors: ['#3987e5'],
              title: `cost by model (${y})`,
              requires_filters: [],
            }}
            rows={report.by_model}
            filters={{}}
          />
        );
      })()}

      <p className="text-[10px] text-[var(--color-text-faint)]">{report.note}</p>
    </div>
  );
}

export default function CostTab() {
  const [pricing, setPricing] = useState<PricingRow[]>([]);
  const [pricingErr, setPricingErr] = useState<string | null>(null);
  const [runIds, setRunIds] = useState<string[]>([]);

  const [runId, setRunId] = useState('');
  const [days, setDays] = useState(7);
  const [report, setReport] = useState<CostReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.costPricing()
      .then((r) => setPricing(r.pricing))
      .catch((e) => setPricingErr((e as Error).message));
    api.listRuns({ limit: 200 })
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {});
    // Default load: empty report (no run_id, last 7 days)
    api.costReport()
      .then(setReport)
      .catch(() => {});
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await api.costReport({ run_id: runId.trim() || undefined, days });
      setReport(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, days]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-[var(--color-text)]">LLM Cost Attribution</h2>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Per-run + per-model token usage and cost (cap-002 · ADR-0066)
          </p>
        </div>
        <div className="flex gap-1.5">
          {(['cap-002', 'ADR-0066', 'v0.19.0'] as const).map((label) => (
            <span
              key={label}
              className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Report</h3>

        <div className="grid grid-cols-3 gap-2 items-end">
          <div className="space-y-1 col-span-2">
            <label className={labelClass}>run_id (optional — empty = all runs)</label>
            <SuggestInput
              value={runId}
              onChange={setRunId}
              suggestions={runIds}
              placeholder="run_2026_..."
              className={inputClass + ' w-full'}
              onEnter={refresh}
            />
          </div>
          <div className="space-y-1">
            <label className={labelClass}>window (days)</label>
            <input
              type="number"
              min={1}
              max={365}
              value={days}
              onChange={(e) => setDays(Math.max(1, Math.min(365, Number(e.target.value) || 7)))}
              className={inputClass + ' w-full'}
            />
          </div>
        </div>

        <button
          onClick={refresh}
          disabled={loading}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
            loading
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'loading…' : 'Run report'}
        </button>

        {err && (
          <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2 font-mono">{err}</div>
        )}

        {report && <ReportPanel report={report} />}
      </section>

      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Price Table</h3>
        <p className="text-[10px] text-[var(--color-text-faint)]">
          Source: <code className="font-mono">novafabric.cost.interceptor.CostInterceptor.PRICE_TABLE</code> · estimates only
        </p>
        {pricingErr && (
          <p className="text-[11px] font-mono text-[var(--color-status-failure)]">{pricingErr}</p>
        )}
        <PricingPanel rows={pricing} />
      </section>

      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Cost analytics tools</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Spend attribution · agent fairness · token usage breakdown (ADR-0146 / ADR-0132)
          </p>
        </div>
        <CostToolsPanel />
      </section>
    </div>
  );
}
