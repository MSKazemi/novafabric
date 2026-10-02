/**
 * Wire types for the Dashboards view — ADR-0235 portable widgets and
 * ADR-0236 `ratio()`. Served by `src/novafabric/serve/routers/dashboards.py`;
 * the declared response models there are the source of truth.
 */

export type WidgetChart = 'line' | 'bar' | 'area' | 'table' | 'stat';

export interface DashboardSummary {
  id: string;
  title: string;
  description: string | null;
  builtin: boolean;
  version: number;
  widget_count: number;
  unresolved_widgets: string[];
  invalid_widgets: string[];
}

export interface WidgetSummary {
  id: string;
  title: string;
  description: string | null;
  /** The schema enum today; a newer file may carry a value this build cannot draw. */
  chart: WidgetChart | string;
  version: number;
}

export interface InvalidDashboardFile {
  file: string;
  kind: 'widget' | 'dashboard' | string;
  error: string;
}

export interface DashboardListResponse {
  dashboards: DashboardSummary[];
  widgets: WidgetSummary[];
  invalid_files: InvalidDashboardFile[];
  cli_equivalent: string;
}

export interface WidgetPresentation {
  chart: WidgetChart | string;
  breakdown?: string;
  unit?: string;
  stacked?: boolean;
  [extra: string]: unknown;
}

export interface WidgetDefinition {
  id: string;
  title: string;
  description: string | null;
  chart: WidgetChart | string;
  version: number;
  presentation: WidgetPresentation;
  query: Record<string, unknown>;
}

export type WidgetRefStatus = 'ok' | 'missing' | 'invalid';

export interface WidgetPosition {
  x?: number;
  y?: number;
  w?: number;
  h?: number;
}

export interface DashboardWidgetRef {
  widget: string;
  position: WidgetPosition | null;
  status: WidgetRefStatus | string;
  definition: WidgetDefinition | null;
  error: string | null;
}

export interface DashboardDetailResponse {
  dashboard: DashboardSummary;
  widgets: DashboardWidgetRef[];
  cli_equivalent: string;
}

/** ADR-0236 D3 — a derived column and the two select items it divides. */
export interface DerivedColumn {
  alias: string;
  func: 'ratio' | string;
  numerator: string;
  denominator: string;
}

export interface WidgetDataResponse {
  widget: WidgetDefinition;
  schema_version: string;
  generated_at: string;
  query: Record<string, unknown>;
  time_window: { since: string | null; until: string | null };
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count: number;
  truncated: boolean;
  index: { engine: string; built_at: string; capsule_count: number };
  tree_scope?: {
    scope: string;
    capsules_selected: number;
    expansion_truncated: boolean;
    incomplete_reasons: string[];
    complete: boolean;
  };
  derived: DerivedColumn[];
  cli_equivalent: string;
}
