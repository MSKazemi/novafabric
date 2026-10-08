/**
 * Live smoke path per major dashboard tab, against a real `nova serve`.
 *
 * Each test asserts one thing a mocked-API spec cannot: that the shipped bundle,
 * the real routes and a real capsule store agree. Skipped (not failed) when no
 * browser or no novafabric interpreter is available; see serve-fixture.ts.
 */
import { test, expect, type Page } from '@playwright/test';
import { skipReason, startServer, type LiveServer } from './serve-fixture';

let server: LiveServer;
const reason = skipReason();

test.describe('live nova serve', () => {
  test.skip(reason !== null, reason ?? '');
  test.describe.configure({ mode: 'serial' });

  test.beforeAll(async () => {
    server = await startServer(2);
  });
  test.afterAll(async () => {
    await server?.stop();
  });

  const open = (page: Page, tab = '') =>
    page.goto(`${server.base}/dashboard?token=${server.token}${tab ? `&tab=${tab}` : ''}`);

  test('home: connects and lists the seeded runs', async ({ page }) => {
    await open(page);
    await expect(page.getByText('connected', { exact: true })).toBeVisible();
    await expect(page.getByText('total runs')).toBeVisible();
    await expect(page.getByText('Recent Activity')).toBeVisible();
    await expect(page.getByText('SUCCESS').first()).toBeVisible();
  });

  test('runs: list, saved view, evidence cart, copy button is not nested', async ({ page }) => {
    await open(page, 'runs');
    const row = page.locator('[class*="group/run"]').first();
    await expect(row).toBeVisible();
    // A <button> inside a <button> is invalid HTML and breaks keyboard/AT use.
    await expect(page.locator('button button')).toHaveCount(0);

    await page.getByPlaceholder(/save current as/i).fill('live-e2e-view');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('button', { name: /live-e2e-view/ }).first()).toBeVisible();

    await row.hover();
    await row.getByRole('button', { name: /to the evidence cart/ }).click();
    await expect(page.getByTestId('evidence-cart')).toContainText('(1)');
    await page.getByTestId('cart-export').click();
    await expect(page.getByRole('dialog', { name: 'Confirm cart export' })).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.getByRole('dialog', { name: 'Confirm cart export' })).toHaveCount(0);
  });

  test('analytics: the runs index is computable for a default serve', async ({ request }) => {
    const r = await request.get(`${server.base}/api/analytics/summary?token=${server.token}`);
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body.aggregate.condition).not.toBe('source_unavailable');
  });

  test('dashboards: empty state names where widgets live', async ({ page }) => {
    await open(page, 'dashboards');
    await expect(page.getByText('No dashboards or widgets installed yet.')).toBeVisible();
  });

  test('evidence: tab renders', async ({ page }) => {
    await open(page, 'evidence');
    await expect(page.getByText(/evidence bundles/i).first()).toBeVisible();
  });

  test('compliance: erasure of an unknown subject FAILS with its reason', async ({ page }) => {
    await open(page, 'compliance');
    await page.getByText(/^Privacy/).first().click();
    await page.getByPlaceholder('subject-001 or run_...').fill('live-e2e-unknown-subject');
    await page.getByRole('button', { name: 'Queue erasure request' }).click();
    await expect(page.getByText('FAILED')).toBeVisible();
    await expect(page.getByText(/subject_not_found/)).toBeVisible();
  });

  test('system health names the registry this server uses', async ({ page }) => {
    await open(page);
    await expect(page.getByText(`${server.home}/registry.db`)).toBeVisible();
  });

  test('tabs load without console errors or failed requests', async ({ page }) => {
    const bad: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') bad.push(`console: ${m.text()}`);
    });
    page.on('pageerror', (e) => bad.push(`pageerror: ${e}`));
    page.on('response', (r) => {
      if (r.status() >= 400) bad.push(`http ${r.status()} ${new URL(r.url()).pathname}`);
    });
    for (const tab of ['analytics', 'cost', 'registry', 'lineage', 'audit', 'holds', 'infra', 'admin']) {
      await open(page, tab);
      await expect(page.getByRole('main')).toBeVisible();
      await page.waitForLoadState('networkidle');
    }
    expect(bad).toEqual([]);
  });
});
