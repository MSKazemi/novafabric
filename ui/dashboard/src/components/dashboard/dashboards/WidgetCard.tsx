/**
 * One widget on a dashboard: loads its data from the widget's own stored
 * query (`GET /api/dashboard-widgets/{id}/data`) and renders it as the file
 * says — or says plainly why it cannot.
 *
 * States, each distinct and each announced:
 *   loading → polite status · refused (422: the file or its query was
 *   rejected) → alert with the validator's reason · error (other failures) →
 *   alert + retry · empty (the query matched nothing — *not* zero) → note ·
 *   data → chart / table / stat, with absent values shown as "no value".
 */
import { useMemo, useState, type ReactNode } from 'react';
import { api, ServeApiError } from '../../../lib/api';
import type { WidgetDataResponse, WidgetDefinition } from '../../../lib/dashboardTypes';
import { useQuery } from '../../../lib/useQuery';
import { useToast } from '../../../lib/ToastContext';
import Badge from '../../ui/primitives/Badge';
import Button from '../../ui/primitives/Button';
import Select from '../../ui/primitives/Select';
import { Skeleton } from '../../ui/Skeleton';
import ValueCell, { NoValue } from './ValueCell';
import WidgetChart, { type DrawableChart } from './WidgetChart';
import {
  KNOWN_CHARTS,
  buildChartModel,
  defaultMeasure,
  derivedFor,
  downloadBlob,
  groupByOf,
  isAbsent,
  measureColumns,
  SERIES_COLORS,
} from './model';

const labelClass = 'text-2xs font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

export function WidgetFrame({
  title,
  badge,
  actions,
  footer,
  children,
  headingId,
}: {
  title: string;
  badge?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  headingId: string;
}) {
  return (
    <section
      aria-labelledby={headingId}
      className="h-full flex flex-col rounded-md border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-[var(--shadow-1)] min-w-0"
    >
      <header className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 border-b border-[var(--color-border)]">
        <div className="flex items-center gap-2 min-w-0">
          <h3 id={headingId} className="text-xs font-semibold text-[var(--color-text)] truncate">
            {title}
          </h3>
          {badge}
        </div>
        {actions && <div className="flex items-center gap-1.5 shrink-0">{actions}</div>}
      </header>
      <div className="p-3 flex-1 min-w-0">{children}</div>
      {footer && (
        <footer className="px-3 py-2 border-t border-[var(--color-border)] text-2xs text-[var(--color-text-faint)] space-y-1">
          {footer}
        </footer>
      )}
    </section>
  );
}

/** A reference that cannot render — missing file or refused file. Never omitted. */
export function UnavailableWidget({
  widgetId,
  status,
  error,
}: {
  widgetId: string;
  status: string;
  error: string | null;
}) {
  const missing = status === 'missing';
  return (
    <WidgetFrame
      headingId={`widget-${widgetId}-title`}
      title={widgetId}
      badge={<Badge tone={missing ? 'pending' : 'danger'} dot>{missing ? 'missing' : 'refused'}</Badge>}
    >
      <div role="alert" className="text-xs text-[var(--color-text-muted)] space-y-1">
        <p>
          {missing
            ? 'This dashboard references a widget that is not installed. The rest of the dashboard is shown; this panel is not.'
            : 'This widget file was refused and is not rendered.'}
        </p>
        {error && <p className="font-mono text-2xs text-[var(--color-text-faint)] break-words">{error}</p>}
        {missing && (
          <p className="font-mono text-2xs text-[var(--color-text-faint)]">
            $ nova dashboard apply ./{widgetId}.widget.json
          </p>
        )}
      </div>
    </WidgetFrame>
  );
}

function DataTableView({ data, unit }: { data: WidgetDataResponse; unit?: string }) {
  const dims = new Set(groupByOf(data));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <caption className="sr-only">{data.widget.title} — data table</caption>
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {data.columns.map((c) => {
              const d = derivedFor(data, c);
              return (
                <th
                  key={c}
                  scope="col"
                  className={`py-1 px-2 font-mono text-2xs font-medium text-[var(--color-text-faint)] ${dims.has(c) ? 'text-left' : 'text-right'}`}
                >
                  {c}
                  {d && (
                    <span className="block normal-case text-[var(--color-text-faint)]">
                      = {d.numerator} ÷ {d.denominator}
                    </span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row, ri) => (
            <tr key={ri} className="border-b border-[var(--color-border)] last:border-b-0">
              {data.columns.map((c) =>
                dims.has(c) ? (
                  <th key={c} scope="row" className="py-1 px-2 text-left font-mono font-normal text-[var(--color-text)]">
                    {isAbsent(row[c]) ? <NoValue /> : String(row[c])}
                  </th>
                ) : (
                  <td key={c} className="py-1 px-2 text-right font-mono text-[var(--color-text)]">
                    <ValueCell value={row[c]} unit={unit} derived={derivedFor(data, c)} row={row} />
                  </td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatView({ data, measure, unit }: { data: WidgetDataResponse; measure: string; unit?: string }) {
  const row = data.rows[0];
  const derived = derivedFor(data, measure);
  return (
    <div className="space-y-2">
      <div className={labelClass}>{measure}</div>
      <div className="text-2xl font-semibold font-mono text-[var(--color-text)]" data-testid="stat-value">
        <ValueCell value={row[measure]} unit={unit} derived={derived} row={row} />
      </div>
      {derived && (
        <p className="text-2xs text-[var(--color-text-faint)]">
          {derived.alias} = {derived.numerator} ÷ {derived.denominator}
        </p>
      )}
    </div>
  );
}

function Legend({ names }: { names: string[] }) {
  if (names.length < 2) return null;
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 text-2xs font-mono text-[var(--color-text-muted)]" aria-label="Series">
      {names.map((n, i) => (
        <li key={n} className="flex items-center gap-1">
          <span aria-hidden="true" className="inline-block w-2 h-2 rounded-sm" style={{ background: SERIES_COLORS[i % SERIES_COLORS.length] }} />
          {n}
        </li>
      ))}
    </ul>
  );
}

function WidgetBody({ data }: { data: WidgetDataResponse }) {
  const presentation = data.widget.presentation;
  const chart = String(presentation.chart);
  const unit = typeof presentation.unit === 'string' ? presentation.unit : undefined;
  const measures = measureColumns(data);
  const [measure, setMeasure] = useState<string | null>(() => defaultMeasure(data));
  const [asTable, setAsTable] = useState(false);
  const selectId = `measure-${data.widget.id}`;

  const model = useMemo(
    () => (measure ? buildChartModel(data, presentation, measure) : null),
    [data, presentation, measure],
  );

  if (data.rows.length === 0) {
    return (
      <p role="note" className="text-xs text-[var(--color-text-muted)]" data-testid="widget-empty">
        No rows matched this widget&apos;s query in the window
        {' '}<span className="font-mono">{data.time_window.since ?? 'all time'} → {data.time_window.until ?? 'now'}</span>.
        {' '}That is no data — not a zero.
      </p>
    );
  }

  if (!(KNOWN_CHARTS as readonly string[]).includes(chart)) {
    return (
      <div className="space-y-2">
        <p role="note" className="text-2xs text-[var(--color-text-muted)]">
          This build cannot draw chart type <span className="font-mono">{chart}</span>; showing the data as a table.
        </p>
        <DataTableView data={data} unit={unit} />
      </div>
    );
  }

  if (chart === 'table') return <DataTableView data={data} unit={unit} />;

  const picker = measures.length > 1 && measure && (
    <div className="flex items-center gap-2">
      <label htmlFor={selectId} className={labelClass}>Measure</label>
      <Select
        id={selectId}
        className="w-auto max-w-[16rem]"
        value={measure}
        onChange={(e) => setMeasure(e.target.value)}
      >
        {measures.map((m) => {
          const d = derivedFor(data, m);
          return <option key={m} value={m}>{d ? `${m} (= ${d.numerator} ÷ ${d.denominator})` : m}</option>;
        })}
      </Select>
    </div>
  );

  if (chart === 'stat') {
    if (!measure) return <DataTableView data={data} unit={unit} />;
    return (
      <div className="space-y-3">
        {picker}
        {data.rows.length > 1 && (
          <p role="note" className="text-2xs text-[var(--color-text-muted)]">
            A stat shows one row; this query returned {data.rows.length}. All rows are listed below.
          </p>
        )}
        <StatView data={data} measure={measure} unit={unit} />
        {data.rows.length > 1 && <DataTableView data={data} unit={unit} />}
      </div>
    );
  }

  if (!measure || !model) return <DataTableView data={data} unit={unit} />;
  const derived = derivedFor(data, measure);
  const stackedIgnored = presentation.stacked === true && chart === 'area' && model.series.length > 1;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {picker || <span className={labelClass}>{measure}</span>}
        <Button
          size="sm"
          variant="ghost"
          aria-pressed={asTable}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? 'Show chart' : 'Show as table'}
        </Button>
      </div>
      {derived && (
        <p className="text-2xs font-mono text-[var(--color-text-faint)]" data-testid="ratio-definition">
          {derived.alias} = {derived.numerator} ÷ {derived.denominator} — operands are listed per row in the table view
        </p>
      )}
      {asTable ? (
        <DataTableView data={data} unit={unit} />
      ) : (
        <>
          <WidgetChart
            model={model}
            kind={chart as DrawableChart}
            stacked={presentation.stacked === true}
            unit={unit}
            label={`${data.widget.title}: ${chart} chart of ${measure} over ${model.categories.length} categories`}
          />
          <Legend names={model.series.map((s) => s.name)} />
        </>
      )}
      {model.absent > 0 && (
        <p role="note" className="text-2xs text-[var(--color-text-muted)]" data-testid="absent-note">
          {model.absent} point{model.absent === 1 ? ' has' : 's have'} no value and {model.absent === 1 ? 'is' : 'are'} drawn
          as a gap, not as zero.
        </p>
      )}
      {stackedIgnored && (
        <p role="note" className="text-2xs text-[var(--color-text-faint)]">Stacking applies to bar charts; areas are overlaid.</p>
      )}
    </div>
  );
}

interface WidgetLoad {
  data: WidgetDataResponse | null;
  refusal: string | null;
}

export default function WidgetCard({
  widget,
  refreshTick = 0,
}: {
  widget: WidgetDefinition;
  refreshTick?: number;
}) {
  const { toast } = useToast();
  const [downloading, setDownloading] = useState(false);
  // 422 = the file or its query was refused by validation (ADR-0235 D7):
  // a refusal, not an outage — retrying will not change it, so it is a
  // result state rather than an error with a Retry button.
  const q = useQuery<WidgetLoad>(async () => {
    try {
      return { data: await api.getWidgetData(widget.id), refusal: null };
    } catch (e) {
      if (e instanceof ServeApiError && e.status === 422) return { data: null, refusal: e.message };
      throw e;
    }
  }, [widget.id, refreshTick]);
  const headingId = `widget-${widget.id}-title`;
  const refusal = q.data?.refusal ?? null;

  async function download() {
    setDownloading(true);
    try {
      const blob = await api.exportDashboardDocument('widget', widget.id);
      downloadBlob(blob, `${widget.id}.widget.json`);
    } catch (e) {
      toast('error', e instanceof ServeApiError || e instanceof Error ? e.message : String(e));
    } finally {
      setDownloading(false);
    }
  }

  const data = q.data?.data ?? null;
  return (
    <WidgetFrame
      headingId={headingId}
      title={widget.title}
      badge={<Badge tone="neutral">{widget.chart}</Badge>}
      actions={
        <Button
          size="sm"
          variant="ghost"
          icon="export"
          pending={downloading}
          onClick={download}
          aria-label={`Download ${widget.title} widget JSON`}
        >
          JSON
        </Button>
      }
      footer={
        data ? (
          <>
            <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono">
              <span>window {data.time_window.since ?? 'all'} → {data.time_window.until ?? 'now'}</span>
              <span>{data.row_count} row{data.row_count === 1 ? '' : 's'}</span>
              <span>{data.index.capsule_count} capsules indexed</span>
            </div>
            {data.truncated && (
              <p role="note" className="text-[var(--color-status-pending)]">
                Truncated — more rows exist than are shown. Narrow the widget&apos;s query or raise its limit.
              </p>
            )}
            {data.tree_scope && !data.tree_scope.complete && (
              <p role="note" className="text-[var(--color-status-pending)]">
                Incomplete {data.tree_scope.scope} scope: {data.tree_scope.incomplete_reasons.join('; ') || 'expansion truncated'}
              </p>
            )}
            <p className="font-mono overflow-x-auto whitespace-nowrap">$ {data.cli_equivalent}</p>
          </>
        ) : undefined
      }
    >
      {widget.description && <p className="mb-2 text-2xs text-[var(--color-text-muted)]">{widget.description}</p>}
      {q.loading && !data && !refusal && (
        <div role="status" aria-live="polite" className="space-y-2">
          <span className="sr-only">Loading {widget.title}…</span>
          <Skeleton height="h-4" width="w-1/3" />
          <Skeleton height="h-32" />
        </div>
      )}
      {refusal && (
        <div role="alert" className="text-xs space-y-1" data-testid="widget-refused">
          <Badge tone="danger" dot>refused</Badge>
          <p className="text-[var(--color-text-muted)]">This widget was refused and is not rendered:</p>
          <p className="font-mono text-2xs text-[var(--color-text-faint)] break-words">{refusal}</p>
        </div>
      )}
      {q.error && (
        <div role="alert" className="text-xs space-y-2" data-testid="widget-error">
          <p className="text-[var(--color-status-failure)]">Could not load this widget: {q.error}</p>
          <Button size="sm" variant="danger" onClick={q.reload}>Retry</Button>
        </div>
      )}
      {data && !q.error && <WidgetBody key={data.generated_at} data={data} />}
    </WidgetFrame>
  );
}
