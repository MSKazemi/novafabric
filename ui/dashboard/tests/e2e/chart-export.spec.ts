import { test, expect, type Page } from '@playwright/test';
import { statSync } from 'node:fs';

// Chart image export (ADR-0201) — Analytics tab SVG/PNG download.
//
// The /dashboard route normally needs a live `nova serve` backend; here the
// connection is faked by seeding the localStorage token/base the app reads
// (src/lib/api.ts) and mocking the handful of API routes the boot sequence
// and the Analytics tab hit. Everything else under /api/** 404s, which the
// app tolerates (sidebar counts are best-effort).

const TOKEN = 'e2e-chart-export-token';

const day = (offset: number) =>
  new Date(Date.now() - offset * 86_400_000).toISOString().slice(0, 10);

const SUMMARY = {
  buckets: [6, 5, 4, 3, 2, 1, 0].map((off, i) => ({
    bucket: day(off),
    run_count: 8 + i,
    failed_count: i % 3,
    model_call_count: 40 + i,
    tool_call_count: 12 + i,
    duration_ms_p50: 900 + i * 120,
    duration_ms_p95: 2400 + i * 300,
    duration_ms_max: 4100 + i * 350,
  })),
  totals: { run_count: 63, failed_count: 6, model_call_count: 301, tool_call_count: 93 },
  since: day(7),
  until: null,
};

async function setupAnalyticsPage(page: Page, opts: { dark?: boolean } = {}): Promise<void> {
  await page.addInitScript(
    ({ token, dark }) => {
      localStorage.setItem('novafabric.serve-token', token);
      // Same-origin base so mocked /api/** routes cover every request.
      localStorage.setItem('novafabric.serve-base', location.origin);
      if (dark) {
        localStorage.setItem('novafabric.theme', 'dark');
        document.documentElement.setAttribute('data-theme', 'dark');
      }
    },
    { token: TOKEN, dark: opts.dark ?? false },
  );

  // Catch-all first; later, more specific routes win (Playwright matches
  // the most recently registered route first).
  await page.route('**/api/**', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'not mocked' }) }),
  );
  await page.route('**/api/health*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, version: 'e2e-test' }) }),
  );
  // Token validation at boot fetches /api/runs and only checks the status.
  await page.route('**/api/runs*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs: [], total: 0 }) }),
  );
  await page.route('**/api/analytics/summary*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SUMMARY) }),
  );

  await page.goto('/dashboard?tab=analytics');
  // Chart rendered ⇒ boot + analytics fetch both succeeded.
  await expect(page.getByRole('img', { name: 'Runs per day' })).toBeVisible();
}

async function clickAndExpectDownload(page: Page, button: 'PNG' | 'SVG', suffix: string): Promise<void> {
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: button, exact: true }).first().click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(`analytics-runs-per-day${suffix}`);
  const path = await download.path();
  expect(path).toBeTruthy();
  expect(statSync(path!).size).toBeGreaterThan(0);
}

test('analytics chart: PNG export downloads a non-empty file', async ({ page }) => {
  await setupAnalyticsPage(page);
  await clickAndExpectDownload(page, 'PNG', '.png');
});

test('analytics chart: SVG export downloads a non-empty file', async ({ page }) => {
  await setupAnalyticsPage(page);
  await clickAndExpectDownload(page, 'SVG', '.svg');
});

test('analytics chart: PNG export also succeeds with the dark theme active', async ({ page }) => {
  await setupAnalyticsPage(page, { dark: true });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await clickAndExpectDownload(page, 'PNG', '.png');
});
