/** Regression for #255: selecting a live palette run must open that exact run. */
import { test, expect } from '@playwright/test';
import { gotoDashboard, makeRun, mockApi, runSearchPage, tabButton, urlParam } from './fixtures/dashboard';

const capsule = (id: string) => ({
  run_id: id, capsule_path: `/capsules/${id}`, manifest: { run_id: id },
  trace: [], model_calls: [], tool_calls: [], lineage: [], assets: [], inputs: [], outputs: [],
});

const inspectorRun = (page: import('@playwright/test').Page) =>
  page.getByRole('main').locator('header code').filter({ hasText: /^run-\d+$/ });

test('live run result from Home opens that run, including an ID outside the initial list', async ({ page }) => {
  await mockApi(page, {
    '/api/runs/run-042': { body: capsule('run-042') },
  });
  // Search results can include a run outside the Runs page already loaded.
  await page.route((url) => url.pathname === '/api/runs/search', async (route) => {
    const q = new URL(route.request().url()).searchParams.get('q');
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(q ? runSearchPage({ items: [makeRun(42)] }) : runSearchPage()),
    });
  });
  await gotoDashboard(page);

  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  await palette.getByRole('textbox', { name: 'Search commands' }).fill('run-042');
  await palette.getByRole('button', { name: /^Run run-042\b/ }).click();

  await expect(tabButton(page, 'Runs')).toHaveAttribute('aria-current', 'page');
  await expect.poll(() => urlParam(page, 'run')).toBe('run-042');
  await expect(inspectorRun(page)).toHaveText('run-042');
  expect(page.url()).not.toContain('token=');
  await page.reload();
  await expect(inspectorRun(page)).toHaveText('run-042');
});

test('live run result changes the mounted Runs inspector, not just the URL', async ({ page }) => {
  await mockApi(page, {
    '/api/runs/run-001': { body: capsule('run-001') },
    '/api/runs/run-002': { body: capsule('run-002') },
  });
  await gotoDashboard(page, 'tab=runs&run=run-001&view=trace');
  await expect(inspectorRun(page)).toHaveText('run-001');

  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  await palette.getByRole('textbox', { name: 'Search commands' }).fill('run-002');
  await palette.getByRole('button', { name: /^Run run-002\b/ }).press('Enter');

  await expect(inspectorRun(page)).toHaveText('run-002');
  await expect.poll(() => urlParam(page, 'run')).toBe('run-002');
  await expect.poll(() => urlParam(page, 'view')).toBeNull();
});

test('palette selection never displays the previous capsule while a new run loads', async ({ page }) => {
  await mockApi(page, {
    '/api/runs/run-001': { body: capsule('run-001') },
  });
  let releaseRequest = null as (() => void) | null;
  // Hold the B response so the stale-A visibility window is deterministic.
  await page.route((url) => url.pathname === '/api/runs/run-002', async (route) => {
    await new Promise<void>((resolve) => { releaseRequest = resolve; });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(capsule('run-002')) });
  });
  await gotoDashboard(page, 'tab=runs&run=run-001');
  await expect(inspectorRun(page)).toHaveText('run-001');

  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  await palette.getByRole('textbox', { name: 'Search commands' }).fill('run-002');
  await palette.getByRole('button', { name: /^Run run-002\b/ }).click();

  await expect.poll(() => urlParam(page, 'run')).toBe('run-002');
  await expect.poll(() => releaseRequest !== null).toBe(true);
  // No A data may remain visible during B's pending API request.
  await expect(inspectorRun(page)).toHaveCount(0);
  if (!releaseRequest) throw new Error('B request not observed');
  releaseRequest();
  await expect(inspectorRun(page)).toHaveText('run-002');
});

test('a mismatched backend run ID fails closed instead of displaying another run', async ({ page }) => {
  await mockApi(page, {
    '/api/runs/run-002': { body: capsule('run-001') },
  });
  await gotoDashboard(page, 'tab=runs&run=run-002');
  await expect(page.getByRole('alert')).toContainText('Run detail does not match the requested run ID.');
  await expect(inspectorRun(page)).toHaveCount(0);
});

test('a denied run result never reuses the previous run inspector', async ({ page }) => {
  await mockApi(page, {
    '/api/runs/run-001': { body: capsule('run-001') },
    '/api/runs/run-003': { status: 403, body: { detail: 'access denied' } },
  });
  await gotoDashboard(page, 'tab=runs&run=run-001');
  await expect(inspectorRun(page)).toHaveText('run-001');

  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  await palette.getByRole('textbox', { name: 'Search commands' }).fill('run-003');
  await palette.getByRole('button', { name: /^Run run-003\b/ }).click();

  await expect.poll(() => urlParam(page, 'run')).toBe('run-003');
  await expect(page.getByRole('alert')).toContainText('Could not load run detail');
  await expect(inspectorRun(page)).toHaveCount(0);
});
