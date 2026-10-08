/**
 * Fixture-driven harness for the authenticated dashboard shell.
 *
 * The `/dashboard` island normally needs a live `nova serve` backend. These
 * helpers replace it with Playwright route interception: every `/api/**`
 * request is answered from a small in-memory table, so the specs need no
 * server process, no real token, and no capsule store.
 *
 * Token mechanism (mirrors `DashboardApp.consumeTokenFromUrl`): the app reads
 * `?token=` from the URL on mount and stashes it via `setConnection()` into
 * localStorage under the keys owned by `src/lib/api.ts`:
 *   - `novafabric.serve-token`
 *   - `novafabric.serve-base`  (set to `window.location.origin`)
 * Because the base becomes the page origin, every subsequent API call is
 * same-origin and therefore covered by the interceptor below.
 */
import type { Page, Route } from '@playwright/test';
import { expect } from '@playwright/test';

export const TOKEN = 'e2e-fixture-token';

/** A canned response: JSON body plus optional non-200 status. */
export interface RouteSpec {
  status?: number;
  body: unknown;
}

/** Path → response. Keys are exact `URL.pathname` values. */
export type RouteTable = Record<string, RouteSpec>;

export const HEALTH = { ok: true, version: 'e2e-fixture' };

export const STATS = {
  run_count: 3,
  failed_run_count: 1,
  passed_run_count: 2,
  asset_count: 2,
  pending_eval_count: 0,
  production_asset_count: 1,
};

export function makeRun(i: number) {
  return {
    run_id: `run-${String(i).padStart(3, '0')}`,
    status: i % 3 === 0 ? 'failed' : 'completed',
    created_at: `2026-07-2${(i % 9) + 1}T10:0${i % 9}:00Z`,
    finished_at: `2026-07-2${(i % 9) + 1}T10:1${i % 9}:00Z`,
    duration_ms: 1200 + i * 10,
    exit_code: i % 3 === 0 ? 1 : 0,
    model_call_count: 2,
    tool_call_count: 1,
    mutating_tool_count: 0,
    command: ['python', `job_${i}.py`],
    novafabric_version: '0.95.0',
    capsule_path: `/capsules/run-${i}`,
  };
}

export const RUNS = [makeRun(1), makeRun(2), makeRun(3)];

/** `/api/runs` — also the endpoint `validateToken()` probes at boot. */
export const RUNS_LIST = {
  count: RUNS.length,
  total: RUNS.length,
  has_more: false,
  limit: 50,
  offset: 0,
  capsule_dir: '/tmp/e2e-capsules',
  runs: RUNS,
};

/** `/api/runs/search` — cursor page shape (`RunSearchPage`). */
export function runSearchPage(opts: {
  items?: ReturnType<typeof makeRun>[];
  nextCursor?: string | null;
  totalApprox?: number;
} = {}) {
  const items = opts.items ?? RUNS;
  return {
    items,
    next_cursor: opts.nextCursor ?? null,
    total_approx: opts.totalApprox ?? items.length,
  };
}

export const HOLDS = { total_active: 1, registries: [] };

export const INCIDENTS = {
  ok: true,
  count: 1,
  incidents: [
    {
      id: 'inc-001',
      title: 'Fixture incident',
      classification: 'serious_incident',
      severity: 'high',
      status: 'open',
      occurred_at: '2026-07-28T08:00:00Z',
      aware_at: '2026-07-28T08:30:00Z',
      run_ids: ['run-001'],
      deadlines: [],
    },
  ],
};

export const ASSETS = {
  count: 1,
  total: 1,
  has_more: false,
  limit: 50,
  offset: 0,
  assets: [
    {
      id: 'asset-1',
      name: 'demo-model',
      version: '1.0.0',
      asset_type: 'model',
      status: 'production',
      created_at: '2026-07-20T09:00:00Z',
      promoted_at: '2026-07-21T09:00:00Z',
      git_commit_sha: null,
    },
  ],
};

export const EVIDENCE = { bundles: [], count: 0, total: 0 };

/** Endpoints the boot sequence + the tabs under test actually hit. */
export function defaultRoutes(): RouteTable {
  return {
    '/api/health': { body: HEALTH },
    '/api/runs': { body: RUNS_LIST },
    '/api/runs/search': { body: runSearchPage() },
    '/api/stats': { body: STATS },
    '/api/holds': { body: HOLDS },
    '/api/incidents': { body: INCIDENTS },
    '/api/assets': { body: ASSETS },
    '/api/evidence': { body: EVIDENCE },
  };
}

/**
 * Intercept every `/api/**` request and answer it from `routes`.
 *
 * One single interceptor (rather than several overlapping globs) keeps
 * matching order irrelevant: unknown paths get a 404 with a JSON `detail`,
 * exactly like the real server, which every tab tolerates.
 */
export async function mockApi(page: Page, overrides: RouteTable = {}): Promise<void> {
  const table: RouteTable = { ...defaultRoutes(), ...overrides };
  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    (route: Route) => {
      const path = new URL(route.request().url()).pathname;
      const spec = table[path];
      if (!spec) {
        return route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: `not mocked: ${path}` }),
        });
      }
      return route.fulfill({
        status: spec.status ?? 200,
        contentType: 'application/json',
        body: JSON.stringify(spec.body),
      });
    },
  );
}

/**
 * Open the dashboard with a token in the URL and wait for the authenticated
 * shell. `query` is appended verbatim (e.g. `tab=compliance&sub=privacy`).
 */
export async function gotoDashboard(page: Page, query = ''): Promise<void> {
  const qs = `token=${TOKEN}${query ? `&${query}` : ''}`;
  await page.goto(`/dashboard?${qs}`);
  await expect(page.getByRole('navigation')).toBeVisible();
}

/** The sidebar nav region (the `<nav role="navigation">` inside the aside). */
export function nav(page: Page) {
  return page.getByRole('navigation');
}

/**
 * A sidebar tab button.
 *
 * Matched on the `title` text the Sidebar gives every tab
 * (`"<Label> — press g <key>"` expanded, `"<Label> (g <key>)"` collapsed).
 * Group headers carry no title, so this never collides with the
 * "Runs & Debug" header the way an accessible-name prefix match would, and
 * it is immune to the badge/count suffixes that ride in the accessible name.
 */
export function tabButton(page: Page, label: string) {
  return nav(page).getByTitle(new RegExp(`^${label}\\b`));
}

/**
 * Sidebar groups collapse by default (only the group holding the active tab is
 * expanded), so a tab in another group is not in the DOM until its header is
 * clicked. Use this wherever a test needs to *click* a tab; `?tab=` deep links
 * and `g`-shortcuts do not need it, because arriving on a tab expands its group.
 */
export async function revealTab(page: Page, label: string): Promise<void> {
  const btn = tabButton(page, label);
  if (await btn.count()) return;
  for (const header of await nav(page).getByRole('button').all()) {
    const text = ((await header.getAttribute('title')) ?? '') + (await header.innerText());
    // Group headers carry no title attribute; tab buttons do.
    if (await header.getAttribute('title')) continue;
    await header.click();
    if (await btn.count()) return;
    await header.click(); // wrong group — collapse it again and keep looking
  }
  throw new Error(`tab "${label}" not reachable from any sidebar group`);
}

/** A sidebar group header (they are buttons that collapse the group). */
export function groupHeader(page: Page, label: string) {
  return nav(page).getByRole('button', { name: label, exact: true });
}

/** The `?<key>=` value currently in the address bar. */
export function urlParam(page: Page, key: string): string | null {
  return new URL(page.url()).searchParams.get(key);
}
