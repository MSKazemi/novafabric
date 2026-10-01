/**
 * Runs-view URL state — ADR-0232 D2: "every view serializes to a URL, and the
 * URL is the whole state".
 *
 * What lives here is *view state* (it decides what the user is looking at, and
 * a colleague opening the link must see the same thing): free-text search,
 * status chip, sort, date window, filter-bar text, ADR-0233 scope, the selected
 * run and the inspector view. What does NOT live here is *transient interaction
 * state* (ADR-0232 D2 carve-out, ADR-0036): the half-finished Compare gesture,
 * checkbox selection, unsubmitted filter text, an open suggestion list.
 *
 * Every reader validates: a hand-edited or stale link degrades to the default
 * rather than rendering a state the UI cannot represent.
 */
import type { DetailView, RunSort, StatusFilter } from './types';

/** URL keys owned by the Runs view; cleared when the user leaves the tab. */
export const RUNS_VIEW_PARAMS = [
  'q', 'status', 'sort', 'since', 'until', 'f', 'scope', 'run', 'view',
] as const;

export type FilterScope = 'node' | 'root' | 'tree';

export const STATUS_FILTERS: readonly StatusFilter[] = ['all', 'running', 'success', 'failure', 'error'];
export const RUN_SORTS: readonly RunSort[] = ['newest', 'oldest', 'longest', 'shortest'];
export const FILTER_SCOPES: readonly FilterScope[] = ['node', 'root', 'tree'];

/**
 * Inspector views that may be deep-linked. `replay` is excluded on purpose:
 * its content is the result of an action run in *this* session, so a link to
 * it would open on nothing.
 */
export const LINKABLE_VIEWS: readonly DetailView[] = [
  'inspect', 'trace', 'secrets', 'forensics', 'children',
];

function oneOf<T extends string>(allowed: readonly T[], raw: string, fallback: T): T {
  return (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
}

export const parseStatus = (raw: string): StatusFilter => oneOf(STATUS_FILTERS, raw, 'all');
export const parseSort = (raw: string): RunSort => oneOf(RUN_SORTS, raw, 'newest');
export const parseScope = (raw: string): FilterScope => oneOf(FILTER_SCOPES, raw, 'node');
export const parseView = (raw: string): DetailView => oneOf(LINKABLE_VIEWS, raw, 'inspect');

/** A `YYYY-MM-DD` date input value, or '' — anything else is dropped. */
export function parseDate(raw: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(raw) ? raw : '';
}

/** Remove every Runs-view key from a query string (used on tab switch). */
export function stripRunsViewParams(params: URLSearchParams): URLSearchParams {
  for (const key of RUNS_VIEW_PARAMS) params.delete(key);
  return params;
}

export const SCOPE_LABEL: Record<FilterScope, string> = {
  node: 'matching runs',
  root: 'root runs containing a match',
  tree: 'whole trees containing a match',
};

/**
 * The dimension being typed at the end of the filter text, if the caret sits
 * inside a `dim:` / `-dim:` term — drives the observed-value suggestions
 * (ADR-0232 D3). Returns the dimension and the partial value typed so far.
 */
export function activeTerm(text: string): { dimension: string; partial: string; negate: boolean } | null {
  const m = /(?:^|\s)(-?)([A-Za-z_][A-Za-z0-9_]*):([^\s"]*)$/.exec(text);
  if (!m) return null;
  return { negate: m[1] === '-', dimension: m[2], partial: m[3] };
}

/** Replace the active term's partial value with `value`, quoting when needed. */
export function completeTerm(text: string, value: string): string {
  const term = activeTerm(text);
  if (!term) return text;
  const quoted = /\s/.test(value) ? `"${value}"` : value;
  const cut = text.length - term.partial.length;
  return `${text.slice(0, cut)}${quoted} `;
}
