/**
 * Dashboards view — ADR-0235 portable widgets, ADR-0236 ratio(), ADR-0234 D2.
 *
 * Acceptance criteria pinned here:
 * - absent/undefined values render "no value", never 0 — and a measured 0 stays 0;
 * - a ratio is shown with its numerator and denominator, and an undefined
 *   ratio says why (zero denominator / absent operand);
 * - a chart draws a missing point as a gap, never at the baseline, and says so;
 * - a refused file is listed with its reason; a missing or refused reference
 *   still gets a panel; an incomplete dashboard is announced;
 * - a 422 from the data endpoint is a refusal (no Retry), other failures are errors;
 * - Download fetches the verbatim file and names it after the id.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '@/lib/ToastContext';
import type {
  DashboardDetailResponse,
  DashboardListResponse,
  WidgetDataResponse,
} from '@/lib/dashboardTypes';
import {
  buildChartModel,
  defaultMeasure,
  formatValue,
  orderedRefs,
  ratioParts,
  spanOf,
  toNumber,
} from '@/components/dashboard/dashboards/model';

const mocks = vi.hoisted(() => {
  class ServeApiError extends Error {
    constructor(public status: number, message: string) {
      super(message);
      this.name = 'ServeApiError';
    }
  }
  return {
    ServeApiError,
    listDashboards: vi.fn(),
    getDashboard: vi.fn(),
    getWidgetData: vi.fn(),
    exportDashboardDocument: vi.fn(),
  };
});

vi.mock('@/lib/api', () => ({
  ServeApiError: mocks.ServeApiError,
  api: {
    listDashboards: mocks.listDashboards,
    getDashboard: mocks.getDashboard,
    getWidgetData: mocks.getWidgetData,
    exportDashboardDocument: mocks.exportDashboardDocument,
  },
}));

import DashboardsView from '@/components/dashboard/dashboards/DashboardsView';
import DashboardsTab from '@/components/dashboard/tabs/DashboardsTab';

const RATIO = { alias: 'cost_per_run', func: 'ratio', numerator: 'sum(cost)', denominator: 'count()' };

function widgetData(over: Partial<WidgetDataResponse> = {}): WidgetDataResponse {
  return {
    widget: {
      id: 'cost-rate',
      title: 'Cost per run',
      description: null,
      chart: 'table',
      version: 1,
      presentation: { chart: 'table' },
      query: { select: ['count()', 'sum(cost)', 'ratio(sum(cost), count()) AS cost_per_run'], group_by: ['status'] },
    },
    schema_version: '1',
    generated_at: '2026-10-02T00:00:00Z',
    query: { group_by: ['status'] },
    time_window: { since: '2026-09-25T00:00:00Z', until: '2026-10-02T00:00:00Z' },
    columns: ['status', 'count()', 'sum(cost)', 'cost_per_run'],
    rows: [
      { status: 'success', 'count()': 4, 'sum(cost)': 0.08, cost_per_run: 0.02 },
      { status: 'failed', 'count()': 2, 'sum(cost)': null, cost_per_run: null },
      { status: 'free', 'count()': 3, 'sum(cost)': 0, cost_per_run: 0 },
    ],
    row_count: 3,
    truncated: false,
    index: { engine: 'sqlite', built_at: '2026-10-02T00:00:00Z', capsule_count: 9 },
    derived: [RATIO],
    cli_equivalent: "nova query --select 'count()'",
    ...over,
  };
}

const LIST: DashboardListResponse = {
  dashboards: [
    {
      id: 'ops', title: 'Ops overview', description: 'Run health', builtin: false, version: 1,
      widget_count: 3, unresolved_widgets: ['gone'], invalid_widgets: ['bad'],
    },
  ],
  widgets: [{ id: 'cost-rate', title: 'Cost per run', description: null, chart: 'table', version: 1 }],
  invalid_files: [{ file: 'evil.widget.json', kind: 'widget', error: "widget 'evil' carries a query the DSL rejects" }],
  cli_equivalent: 'nova dashboard list',
};

const DETAIL: DashboardDetailResponse = {
  dashboard: LIST.dashboards[0],
  widgets: [
    { widget: 'bad', position: { x: 6, y: 0, w: 6, h: 4 }, status: 'invalid', definition: null, error: 'widget is version 9' },
    { widget: 'cost-rate', position: { x: 0, y: 0, w: 6, h: 4 }, status: 'ok', definition: widgetData().widget, error: null },
    { widget: 'gone', position: null, status: 'missing', definition: null, error: 'no widget file gone.widget.json' },
  ],
  cli_equivalent: 'nova dashboard show ops',
};

function renderView() {
  return render(
    <ToastProvider>
      <DashboardsView />
    </ToastProvider>,
  );
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe('dashboards model', () => {
  it('never turns absence into zero, and keeps a measured zero', () => {
    expect(toNumber(null)).toBeNull();
    expect(toNumber(undefined)).toBeNull();
    expect(toNumber('')).toBeNull();
    expect(toNumber(0)).toBe(0);
    expect(formatValue(null)).toBe('—');
    expect(formatValue(0)).toBe('0');
    expect(formatValue(0, 'USD')).toBe('0 USD');
    expect(formatValue(0.02)).toBe('0.02');
    expect(formatValue(12345)).toBe('12.3K');
  });

  it('explains why a ratio has no value', () => {
    expect(ratioParts(RATIO, { 'sum(cost)': 1, 'count()': 0, cost_per_run: null }).reason).toMatch(/zero denominator/);
    expect(ratioParts(RATIO, { 'sum(cost)': null, 'count()': 2, cost_per_run: null }).reason).toBe('numerator absent');
    expect(ratioParts(RATIO, { 'sum(cost)': 0, 'count()': 3, cost_per_run: 0 }).reason).toBeNull();
  });

  it('defaults to the ratio measure and reports gaps instead of zero-filling', () => {
    const data = widgetData();
    expect(defaultMeasure(data)).toBe('cost_per_run');
    const model = buildChartModel(data, { chart: 'bar' }, 'cost_per_run');
    expect(model.categories).toEqual(['success', 'failed', 'free']);
    expect(model.series[0].points.map((p) => p.y)).toEqual([0.02, null, 0]);
    expect(model.absent).toBe(1);
  });

  it('pivots the breakdown dimension into series', () => {
    const data = widgetData({
      query: { group_by: ['day', 'status'] },
      columns: ['day', 'status', 'count()'],
      derived: [],
      rows: [
        { day: 'd1', status: 'ok', 'count()': 1 },
        { day: 'd1', status: 'err', 'count()': 2 },
        { day: 'd2', status: 'ok', 'count()': 3 },
      ],
    });
    const model = buildChartModel(data, { chart: 'bar', breakdown: 'status' }, 'count()');
    expect(model.categories).toEqual(['d1', 'd2']);
    expect(model.series.map((s) => s.name)).toEqual(['ok', 'err']);
    // d2 has no `err` row: that is no data, not zero.
    expect(model.series[1].points.map((p) => p.y)).toEqual([2, null]);
    expect(model.absent).toBe(1);
  });

  it('orders panels top-to-bottom, left-to-right and clamps the span', () => {
    expect(orderedRefs(DETAIL.widgets).map((r) => r.widget)).toEqual(['cost-rate', 'bad', 'gone']);
    expect(spanOf(DETAIL.widgets[0])).toBe(6);
    expect(spanOf(DETAIL.widgets[2])).toBe(12);
  });
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

describe('DashboardsView', () => {
  beforeEach(() => {
    mocks.listDashboards.mockReset().mockResolvedValue(LIST);
    mocks.getDashboard.mockReset().mockResolvedValue(DETAIL);
    mocks.getWidgetData.mockReset().mockResolvedValue(widgetData());
    mocks.exportDashboardDocument.mockReset();
  });

  it('lists dashboards, widgets and refused files with their reason', async () => {
    renderView();
    const nav = await screen.findByRole('navigation', { name: 'Dashboards and widgets' });
    await waitFor(() =>
      expect(within(nav).getByRole('button', { name: /Ops overview/ })).toHaveAttribute('aria-current', 'true'),
    );
    expect(within(nav).getByRole('button', { name: /Cost per run/ })).toBeInTheDocument();
    const refused = within(nav).getByTestId('invalid-files');
    expect(refused).toHaveTextContent('evil.widget.json');
    expect(refused).toHaveTextContent('DSL rejects');
  });

  it('renders every reference — missing and refused ones as explained panels', async () => {
    renderView();
    expect(await screen.findByText(/Incomplete: 2 of 3 referenced widgets/)).toBeInTheDocument();
    const list = screen.getByRole('list', { name: 'Ops overview widgets' });
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(3);
    expect(within(items[1]).getByText('refused')).toBeInTheDocument();
    expect(within(items[1]).getByText('widget is version 9')).toBeInTheDocument();
    expect(within(items[2]).getByText('missing')).toBeInTheDocument();
  });

  it('shows ratio operands and no-value cells, keeping a measured zero', async () => {
    renderView();
    const table = await screen.findByRole('table');
    expect(within(table).getByText('= sum(cost) ÷ count()')).toBeInTheDocument();
    const failed = within(table).getByRole('row', { name: /failed/ });
    // The undefined ratio and the absent sum both read "no value", with the reason.
    expect(within(failed).getAllByTestId('no-value').length).toBeGreaterThanOrEqual(2);
    expect(failed).toHaveTextContent('numerator absent');
    expect(failed).not.toHaveTextContent(/\b0\b/);
    const free = within(table).getByRole('row', { name: /free/ });
    expect(within(free).queryAllByTestId('no-value')).toHaveLength(0);
    expect(free).toHaveTextContent('0');
    expect(within(table).getAllByTestId('ratio-operands')).toHaveLength(3);
  });

  it('draws a missing bar as a gap marker and says so', async () => {
    mocks.getWidgetData.mockResolvedValue(
      widgetData({ widget: { ...widgetData().widget, chart: 'bar', presentation: { chart: 'bar' } } }),
    );
    renderView();
    const chart = await screen.findByTestId('widget-chart');
    expect(chart).toHaveAttribute('role', 'img');
    expect(within(chart).getAllByTestId('chart-no-value')).toHaveLength(1);
    expect(screen.getByTestId('absent-note')).toHaveTextContent('1 point has no value');
    expect(screen.getByTestId('ratio-definition')).toHaveTextContent('cost_per_run = sum(cost) ÷ count()');
  });

  it('switches the chart to an accessible table on request', async () => {
    const user = userEvent.setup();
    mocks.getWidgetData.mockResolvedValue(
      widgetData({ widget: { ...widgetData().widget, chart: 'line', presentation: { chart: 'line' } } }),
    );
    renderView();
    const toggle = await screen.findByRole('button', { name: 'Show as table' });
    await user.click(toggle);
    expect(screen.getByRole('button', { name: 'Show chart' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('table')).toBeInTheDocument();
  });

  it('says an empty result is no data, not zero', async () => {
    mocks.getWidgetData.mockResolvedValue(widgetData({ rows: [], row_count: 0 }));
    renderView();
    expect(await screen.findByTestId('widget-empty')).toHaveTextContent('not a zero');
  });

  it('treats a 422 as a refusal without Retry, other failures as retryable errors', async () => {
    mocks.getWidgetData.mockRejectedValueOnce(new mocks.ServeApiError(422, 'widget is version 2; Refusing'));
    const { unmount } = renderView();
    const refused = await screen.findByTestId('widget-refused');
    expect(refused).toHaveTextContent('Refusing');
    expect(within(refused).queryByRole('button', { name: 'Retry' })).toBeNull();
    unmount();

    mocks.getWidgetData.mockRejectedValueOnce(new mocks.ServeApiError(500, 'index broke'));
    renderView();
    const error = await screen.findByTestId('widget-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(within(error).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('downloads the verbatim dashboard file named after its id', async () => {
    const user = userEvent.setup();
    const blob = new Blob(['{}'], { type: 'application/json' });
    mocks.exportDashboardDocument.mockResolvedValue(blob);
    const createURL = vi.fn(() => 'blob:x');
    const revokeURL = vi.fn();
    Object.assign(URL, { createObjectURL: createURL, revokeObjectURL: revokeURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    renderView();
    await user.click(await screen.findByRole('button', { name: 'Download Ops overview dashboard JSON' }));
    await waitFor(() => expect(mocks.exportDashboardDocument).toHaveBeenCalledWith('dashboard', 'ops'));
    expect(createURL).toHaveBeenCalledWith(blob);
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it('opens a standalone widget from the list', async () => {
    const user = userEvent.setup();
    renderView();
    const nav = await screen.findByRole('navigation', { name: 'Dashboards and widgets' });
    await user.click(within(nav).getByRole('button', { name: /Cost per run/ }));
    expect(await screen.findByText('$ nova dashboard show cost-rate')).toBeInTheDocument();
    expect(mocks.getWidgetData).toHaveBeenCalledWith('cost-rate');
  });

  it('empty store explains how to install a widget', async () => {
    mocks.listDashboards.mockResolvedValue({ dashboards: [], widgets: [], invalid_files: [], cli_equivalent: 'nova dashboard list' });
    renderView();
    expect(await screen.findByText('No dashboards or widgets installed yet.')).toBeInTheDocument();
    expect(screen.getByText(/nova dashboard apply/)).toBeInTheDocument();
  });

  it('the tab renders inside the shared shell', async () => {
    render(
      <ToastProvider>
        <DashboardsTab />
      </ToastProvider>,
    );
    expect(screen.getByRole('heading', { name: 'Dashboards' })).toBeInTheDocument();
    expect(await screen.findByRole('navigation', { name: 'Dashboards and widgets' })).toBeInTheDocument();
  });
});
