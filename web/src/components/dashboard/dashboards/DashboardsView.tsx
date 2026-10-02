/**
 * Dashboards view — ADR-0235 portable widget files, ADR-0236 ratio() metrics.
 *
 * Read-only over `$NOVAFABRIC_HOME/dashboards`: the same files
 * `nova dashboard list|show|export` manage. Installing or editing stays in
 * the CLI (`nova dashboard apply`), which validates a whole directory before
 * writing any of it.
 *
 * Honesty rules this view keeps:
 * - a refused file is listed by name with the validator's reason, never
 *   dropped (one bad paste must not blank the page, nor vanish);
 * - a dashboard reference that is missing or refused still gets its panel,
 *   saying so — a dashboard showing three of four panels is not "the" dashboard;
 * - absent and undefined values read "no value", never 0 (ADR-0234 D2).
 */
import { useEffect, useState, type ReactNode } from 'react';
import { api } from '../../../lib/api';
import type { DashboardSummary, WidgetSummary } from '../../../lib/dashboardTypes';
import { useQuery } from '../../../lib/useQuery';
import { useToast } from '../../../lib/ToastContext';
import Badge from '../../ui/primitives/Badge';
import Button from '../../ui/primitives/Button';
import EmptyState from '../../ui/EmptyState';
import { SkeletonRows } from '../../ui/Skeleton';
import { ErrorBox, Loading } from '../helpers';
import WidgetCard, { UnavailableWidget } from './WidgetCard';
import { downloadBlob, orderedRefs, spanOf } from './model';

type Selection = { kind: 'dashboard'; id: string } | { kind: 'widget'; id: string } | null;

const sectionLabel = 'px-2 pb-1 text-2xs font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

function NavButton({
  selected,
  onClick,
  title,
  meta,
  badges,
}: {
  selected: boolean;
  onClick: () => void;
  title: string;
  meta: string;
  badges?: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={selected ? 'true' : undefined}
      className={`w-full text-left rounded px-2 py-1.5 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)] ${
        selected
          ? 'bg-[var(--color-accent-tint)] text-[var(--color-text)]'
          : 'text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)]'
      }`}
    >
      <span className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium truncate">{title}</span>
        {badges}
      </span>
      <span className="block text-2xs font-mono text-[var(--color-text-faint)] truncate">{meta}</span>
    </button>
  );
}

function DashboardDetail({ id, refreshTick }: { id: string; refreshTick: number }) {
  const { toast } = useToast();
  const [downloading, setDownloading] = useState(false);
  const q = useQuery(() => api.getDashboard(id), [id, refreshTick]);

  if (q.loading && !q.data) return <Loading />;
  if (q.error) return <ErrorBox message={q.error} onRetry={q.reload} />;
  if (!q.data) return null;
  const { dashboard, widgets } = q.data;
  const problems = dashboard.unresolved_widgets.length + dashboard.invalid_widgets.length;

  async function download() {
    setDownloading(true);
    try {
      downloadBlob(await api.exportDashboardDocument('dashboard', id), `${id}.dashboard.json`);
    } catch (e) {
      toast('error', e instanceof Error ? e.message : String(e));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="space-y-3 min-w-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">{dashboard.title}</h3>
            {dashboard.builtin && <Badge tone="info">built-in</Badge>}
            <Badge tone="neutral">v{dashboard.version}</Badge>
          </div>
          {dashboard.description && (
            <p className="mt-1 text-xs text-[var(--color-text-muted)] max-w-prose">{dashboard.description}</p>
          )}
          <p className="mt-1 text-2xs font-mono text-[var(--color-text-faint)]">$ {q.data.cli_equivalent}</p>
        </div>
        <Button icon="export" pending={downloading} onClick={download} aria-label={`Download ${dashboard.title} dashboard JSON`}>
          Download JSON
        </Button>
      </div>

      {problems > 0 && (
        <div role="status" className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[var(--color-pending-tint)] px-3 py-2 text-xs text-[var(--color-status-pending)]">
          Incomplete: {problems} of {dashboard.widget_count} referenced widget{dashboard.widget_count === 1 ? '' : 's'} cannot
          be rendered
          {dashboard.unresolved_widgets.length > 0 && <> — missing: <span className="font-mono">{dashboard.unresolved_widgets.join(', ')}</span></>}
          {dashboard.invalid_widgets.length > 0 && <> — refused: <span className="font-mono">{dashboard.invalid_widgets.join(', ')}</span></>}
          . Their panels say why.
        </div>
      )}

      {widgets.length === 0 ? (
        <EmptyState message="This dashboard references no widgets." cliCommand="nova dashboard apply ./my.dashboard.json" />
      ) : (
        <ul className="grid grid-cols-1 md:grid-cols-12 gap-3" aria-label={`${dashboard.title} widgets`}>
          {orderedRefs(widgets).map((ref) => (
            <li
              key={ref.widget}
              className="min-w-0 md:[grid-column:span_var(--span)_/_span_var(--span)]"
              style={{ ['--span' as string]: spanOf(ref) }}
            >
              {ref.status === 'ok' && ref.definition ? (
                <WidgetCard widget={ref.definition} refreshTick={refreshTick} />
              ) : (
                <UnavailableWidget widgetId={ref.widget} status={ref.status} error={ref.error} />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StandaloneWidget({ summary, refreshTick }: { summary: WidgetSummary; refreshTick: number }) {
  return (
    <div className="space-y-2">
      <p className="text-2xs font-mono text-[var(--color-text-faint)]">$ nova dashboard show {summary.id}</p>
      <WidgetCard
        refreshTick={refreshTick}
        widget={{
          id: summary.id,
          title: summary.title,
          description: summary.description,
          chart: summary.chart,
          version: summary.version,
          presentation: { chart: summary.chart },
          query: {},
        }}
      />
    </div>
  );
}

export default function DashboardsView({ refreshTick = 0 }: { refreshTick?: number }) {
  const list = useQuery(() => api.listDashboards(), [refreshTick]);
  const [selection, setSelection] = useState<Selection>(null);

  // Select the first dashboard (else widget) once the list arrives, and drop a
  // selection whose file disappeared on refresh.
  useEffect(() => {
    const data = list.data;
    if (!data) return;
    const exists =
      selection &&
      (selection.kind === 'dashboard'
        ? data.dashboards.some((d) => d.id === selection.id)
        : data.widgets.some((w) => w.id === selection.id));
    if (exists) return;
    if (data.dashboards[0]) setSelection({ kind: 'dashboard', id: data.dashboards[0].id });
    else if (data.widgets[0]) setSelection({ kind: 'widget', id: data.widgets[0].id });
    else setSelection(null);
  }, [list.data, selection]);

  if (list.loading && !list.data) return <SkeletonRows rows={4} />;
  if (list.error) return <ErrorBox message={list.error} onRetry={list.reload} />;
  const data = list.data;
  if (!data) return null;

  const { dashboards, widgets, invalid_files: invalid } = data;
  const nothing = dashboards.length === 0 && widgets.length === 0;

  const dashMeta = (d: DashboardSummary) => {
    const bad = d.unresolved_widgets.length + d.invalid_widgets.length;
    return `${d.id} · ${d.widget_count} widget${d.widget_count === 1 ? '' : 's'}${bad ? ` · ${bad} unavailable` : ''}`;
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[16rem_minmax(0,1fr)] gap-4">
      <nav aria-label="Dashboards and widgets" className="space-y-4 min-w-0">
        {dashboards.length > 0 && (
          <div>
            <h3 className={sectionLabel}>Dashboards ({dashboards.length})</h3>
            <ul className="space-y-0.5">
              {dashboards.map((d) => (
                <li key={d.id}>
                  <NavButton
                    selected={selection?.kind === 'dashboard' && selection.id === d.id}
                    onClick={() => setSelection({ kind: 'dashboard', id: d.id })}
                    title={d.title}
                    meta={dashMeta(d)}
                    badges={
                      d.unresolved_widgets.length + d.invalid_widgets.length > 0 ? (
                        <Badge tone="pending" dot>incomplete</Badge>
                      ) : d.builtin ? (
                        <Badge tone="info">built-in</Badge>
                      ) : undefined
                    }
                  />
                </li>
              ))}
            </ul>
          </div>
        )}
        {widgets.length > 0 && (
          <div>
            <h3 className={sectionLabel}>Widgets ({widgets.length})</h3>
            <ul className="space-y-0.5">
              {widgets.map((w) => (
                <li key={w.id}>
                  <NavButton
                    selected={selection?.kind === 'widget' && selection.id === w.id}
                    onClick={() => setSelection({ kind: 'widget', id: w.id })}
                    title={w.title}
                    meta={`${w.id} · ${w.chart}`}
                  />
                </li>
              ))}
            </ul>
          </div>
        )}
        {invalid.length > 0 && (
          <div role="status" aria-label="Refused files" data-testid="invalid-files">
            <h3 className={sectionLabel}>Refused files ({invalid.length})</h3>
            <ul className="space-y-1">
              {invalid.map((f) => (
                <li key={f.file} className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[var(--color-danger-tint)] px-2 py-1.5">
                  <span className="flex items-center gap-1.5">
                    <Badge tone="danger" dot>refused</Badge>
                    <span className="text-xs font-mono text-[var(--color-text)] truncate">{f.file}</span>
                  </span>
                  <span className="block mt-0.5 text-2xs font-mono text-[var(--color-text-muted)] break-words">{f.error}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </nav>

      <div className="min-w-0">
        {nothing ? (
          <EmptyState
            message={invalid.length > 0 ? 'No dashboard or widget file passed validation.' : 'No dashboards or widgets installed yet.'}
            hint={
              <>
                Widgets are portable JSON files under <code className="font-mono">$NOVAFABRIC_HOME/dashboards</code>.
                Install one with <code className="font-mono">nova dashboard apply ./my.widget.json</code>.
              </>
            }
          />
        ) : selection?.kind === 'dashboard' ? (
          <DashboardDetail key={selection.id} id={selection.id} refreshTick={refreshTick} />
        ) : selection?.kind === 'widget' ? (
          (() => {
            const w = widgets.find((x) => x.id === selection.id);
            return w ? <StandaloneWidget key={w.id} summary={w} refreshTick={refreshTick} /> : null;
          })()
        ) : null}
      </div>
    </div>
  );
}
