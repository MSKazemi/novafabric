import { useState, useCallback, useEffect } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import '../../../styles/reports-print.css';

interface ReportDef {
  id: string;
  label: string;
  audience: 'Developer' | 'Ops' | 'Compliance' | 'Management';
  description: string;
  endpoint: string;
  filters: FilterDef[];
}

interface FilterDef {
  key: string;
  label: string;
  type: 'date' | 'text' | 'select';
  options?: string[];
}

const DATE_FILTERS: FilterDef[] = [
  { key: 'from', label: 'From', type: 'date' },
  { key: 'to',   label: 'To',   type: 'date' },
];

const CATALOG: ReportDef[] = [
  {
    id: 'run-history', label: 'Run History', audience: 'Developer',
    description: 'All captured runs filtered by date, status, or agent command.',
    endpoint: 'run-history',
    filters: [
      ...DATE_FILTERS,
      { key: 'status', label: 'Status', type: 'select', options: ['all', 'ok', 'error', 'failed'] },
      { key: 'agent',  label: 'Agent',  type: 'text' },
    ],
  },
  {
    id: 'eval-regression', label: 'Eval Regression', audience: 'Developer',
    description: 'Evaluation suite scores over time. Spot regressions across releases.',
    endpoint: 'eval-regression',
    filters: [...DATE_FILTERS, { key: 'suite', label: 'Suite name', type: 'text' }],
  },
  {
    id: 'capsule-compare', label: 'Capsule Compare', audience: 'Developer',
    description: 'Field-by-field comparison of two run capsules.',
    endpoint: 'capsule-compare',
    filters: [
      { key: 'run_a', label: 'Run A', type: 'text' },
      { key: 'run_b', label: 'Run B', type: 'text' },
    ],
  },
  {
    id: 'cost-burn', label: 'Cost Burn', audience: 'Ops',
    description: 'Model calls and token counts grouped by agent command.',
    endpoint: 'cost-burn',
    filters: DATE_FILTERS,
  },
  {
    id: 'throughput', label: 'Throughput', audience: 'Ops',
    description: 'Run count and success rate bucketed by time window.',
    endpoint: 'throughput',
    filters: [
      ...DATE_FILTERS,
      { key: 'resolution', label: 'Resolution', type: 'select', options: ['1h', '1d', '1w'] },
    ],
  },
  {
    id: 'evidence-inventory', label: 'Evidence Inventory', audience: 'Compliance',
    description: 'All evidence bundles with integrity verification status.',
    endpoint: 'evidence-inventory',
    filters: DATE_FILTERS,
  },
  {
    id: 'policy-audit', label: 'Policy Audit', audience: 'Compliance',
    description: 'Policy check results, violations, and remediation status.',
    endpoint: 'policy-audit',
    filters: [
      ...DATE_FILTERS,
      { key: 'policy_id', label: 'Policy ID', type: 'text' },
      { key: 'result',    label: 'Result',    type: 'select', options: ['', 'pass', 'fail', 'warn'] },
    ],
  },
  {
    id: 'seal-verification', label: 'Seal Verification', audience: 'Compliance',
    description: 'Seal proposal status across all capsules.',
    endpoint: 'seal-verification',
    filters: DATE_FILTERS,
  },
  {
    id: 'executive-summary', label: 'Executive Summary', audience: 'Management',
    description: 'High-level KPIs: total runs, success rate, model and tool call volumes.',
    endpoint: 'executive-summary',
    filters: DATE_FILTERS,
  },
  {
    id: 'release-comparison', label: 'Release Comparison', audience: 'Management',
    description: 'Compare eval scores between two NovaFabric version tags.',
    endpoint: 'release-comparison',
    filters: [
      { key: 'version_a', label: 'Version A', type: 'text' },
      { key: 'version_b', label: 'Version B', type: 'text' },
    ],
  },
];

const AUDIENCES = ['Developer', 'Ops', 'Compliance', 'Management'] as const;

export default function ReportsTab() {
  const [selected, setSelected] = useState<ReportDef>(CATALOG[0]);
  const [filterValues, setFilterValues] = useState<Record<string, string>>({});
  const [rows, setRows] = useState<Record<string, unknown>[] | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectReport = useCallback((def: ReportDef) => {
    setSelected(def);
    setFilterValues({});
    setRows(null);
    setColumns([]);
    setError(null);
  }, []);

  const runReport = useCallback(async (def: ReportDef, filters: Record<string, string>) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.reports.fetch(def.endpoint, filters, 'json') as {
        columns: string[];
        rows: Record<string, unknown>[];
        count: number;
      };
      setColumns(result.columns);
      setRows(result.rows);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void runReport(selected, {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  const downloadCsv = useCallback(async () => {
    try {
      const blob = await api.reports.fetch(selected.endpoint, filterValues, 'csv') as Blob;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `${selected.id}.csv`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    }
  }, [selected, filterValues]);

  const downloadJson = useCallback(async () => {
    try {
      const result = await api.reports.fetch(selected.endpoint, filterValues, 'json') as {
        columns: string[]; rows: Record<string, unknown>[]; count: number;
      };
      const blob = new Blob([JSON.stringify(result.rows, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `${selected.id}.json`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    }
  }, [selected, filterValues]);

  const printPdf = useCallback(() => { window.print(); }, []);

  return (
    <div className="flex h-full gap-0 rounded-lg border border-[var(--color-border)] overflow-hidden bg-[var(--color-bg-raised)]" style={{ minHeight: 520 }}>

      {/* Left — catalog */}
      <aside className="w-44 shrink-0 border-r border-[var(--color-border)] bg-[var(--color-bg-sunken)] overflow-y-auto no-print">
        {AUDIENCES.map(audience => (
          <div key={audience}>
            <div className="px-3 pt-3 pb-0.5 text-[9px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] select-none">
              {audience}
            </div>
            {CATALOG.filter(r => r.audience === audience).map(def => (
              <button
                key={def.id}
                type="button"
                onClick={() => selectReport(def)}
                className={clsx(
                  'relative w-full text-left px-3 py-2 text-xs transition-colors',
                  selected.id === def.id
                    ? 'bg-[var(--color-bg-raised)] text-[var(--color-text)]'
                    : 'text-[var(--color-text-muted)] hover:bg-[var(--color-bg-raised)] hover:text-[var(--color-text)]',
                )}
              >
                {selected.id === def.id && (
                  <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r bg-[var(--color-accent)]" aria-hidden="true" />
                )}
                <span className="font-medium">{def.label}</span>
              </button>
            ))}
          </div>
        ))}
      </aside>

      {/* Right — config + preview */}
      <section className="flex-1 flex flex-col overflow-hidden nova-report-print">

        {/* Header */}
        <header className="flex items-center justify-between px-4 py-3 border-b border-[var(--color-border)] bg-[var(--color-bg-raised)] shrink-0 no-print">
          <div>
            <h2 className="text-sm font-semibold text-[var(--color-text)]">{selected.label}</h2>
            <p className="text-xs text-[var(--color-text-faint)] mt-0.5">{selected.description}</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={downloadCsv}
              className="px-2.5 py-1 rounded border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] transition-colors"
            >CSV</button>
            <button
              type="button"
              onClick={downloadJson}
              className="px-2.5 py-1 rounded border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] transition-colors"
            >JSON</button>
            <button
              type="button"
              onClick={printPdf}
              className="px-2.5 py-1 rounded border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] transition-colors"
            >PDF</button>
            <button
              type="button"
              onClick={() => void runReport(selected, filterValues)}
              disabled={loading}
              className="px-3 py-1 rounded bg-[var(--color-accent)] text-[var(--color-accent-fg)] text-xs font-medium hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-50"
            >
              {loading ? 'Loading…' : 'Run report'}
            </button>
          </div>
        </header>

        {/* Filters */}
        <div className="flex items-center gap-3 px-4 py-2.5 border-b border-[var(--color-border)] bg-[var(--color-bg)] shrink-0 flex-wrap no-print">
          {selected.filters.map(f => (
            <label key={f.key} className="flex items-center gap-1.5 text-xs">
              <span className="text-[var(--color-text-faint)] shrink-0">{f.label}</span>
              {f.type === 'select' ? (
                <select
                  value={filterValues[f.key] ?? ''}
                  onChange={e => setFilterValues(p => ({ ...p, [f.key]: e.target.value }))}
                  className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-0.5 text-xs text-[var(--color-text)] focus:border-[var(--color-accent)] focus:outline-none"
                >
                  {(f.options ?? []).map(o => <option key={o} value={o}>{o || 'any'}</option>)}
                </select>
              ) : (
                <input
                  type={f.type === 'date' ? 'date' : 'text'}
                  value={filterValues[f.key] ?? ''}
                  onChange={e => setFilterValues(p => ({ ...p, [f.key]: e.target.value }))}
                  className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-0.5 text-xs font-mono text-[var(--color-text)] focus:border-[var(--color-accent)] focus:outline-none w-32"
                  placeholder={f.type === 'date' ? 'YYYY-MM-DD' : ''}
                />
              )}
            </label>
          ))}
        </div>

        {/* Print-only header */}
        <div className="print-header" style={{ display: 'none' }}>
          <h1>{selected.label}</h1>
          <p>NovaFabric · Generated {new Date().toISOString()}</p>
        </div>

        {/* Preview table */}
        <div className="flex-1 overflow-auto p-4">
          {error && (
            <p className="text-sm text-[var(--color-status-failure)]">{error}</p>
          )}
          {!error && rows === null && !loading && (
            <p className="text-xs text-[var(--color-text-faint)]">Select filters and run the report.</p>
          )}
          {loading && (
            <p className="text-xs text-[var(--color-text-faint)]">Loading…</p>
          )}
          {!loading && rows !== null && rows.length === 0 && (
            <p className="text-xs text-[var(--color-text-faint)]">No data matches the current filters.</p>
          )}
          {!loading && rows !== null && rows.length > 0 && (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-xs border-collapse">
                  <thead>
                    <tr>
                      {columns.map(c => (
                        <th key={c} className="text-left px-2 py-1.5 border-b border-[var(--color-border)] text-[var(--color-text-faint)] font-medium text-[9px] uppercase tracking-wider whitespace-nowrap">
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 200).map((row, i) => (
                      <tr key={i} className="hover:bg-[var(--color-bg-raised)] transition-colors">
                        {columns.map(c => (
                          <td key={c} className="px-2 py-1.5 border-b border-[var(--color-border)]/40 text-[var(--color-text-muted)] font-mono whitespace-nowrap max-w-[240px] truncate">
                            {row[c] === null || row[c] === undefined
                              ? <span className="text-[var(--color-text-faint)]">—</span>
                              : typeof row[c] === 'boolean'
                                ? <span className={row[c] ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>{String(row[c])}</span>
                                : String(row[c])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-2 text-[10px] text-[var(--color-text-faint)]">
                {rows.length} row{rows.length !== 1 ? 's' : ''}{rows.length > 200 ? ' (showing first 200)' : ''}
              </p>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
