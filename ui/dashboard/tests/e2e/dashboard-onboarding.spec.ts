/**
 * #265 — first-run onboarding on Home while the store has no runs.
 *
 * Copy-only: the panel builds a `nova capture` command, never executes it and never
 * writes to the server. It appears only when /api/stats reports exactly zero runs — a
 * refused (null) count or real runs must not show it. Served from fixtures, no server.
 */
import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { STATS, RUNS_LIST, mockApi, gotoDashboard } from './fixtures/dashboard';

const EMPTY = {
  '/api/stats': { body: { ...STATS, run_count: 0, failed_run_count: 0, passed_run_count: 0 } },
  '/api/runs': { body: { ...RUNS_LIST, runs: [], count: 0, total: 0 } },
};

function panel(page: Page) {
  return page.getByRole('region', { name: 'Capture your first run' });
}

test.describe('first-run onboarding (#265)', () => {
  test('shows the guide and a copy-only command when there are no runs', async ({ page }) => {
    await mockApi(page, EMPTY);
    await gotoDashboard(page);

    await expect(panel(page)).toBeVisible();
    await expect(page.getByTestId('onboarding-command')).toHaveText(
      `nova capture --output-dir ${RUNS_LIST.capsule_dir} -- python agent.py`,
    );
    await expect(panel(page).getByText(/does not run this command and sends nothing to the server/)).toBeVisible();
  });

  test('is operable from the keyboard and never sends a write request', async ({ page }) => {
    const writes: string[] = [];
    page.on('request', (r) => {
      if (new URL(r.url()).pathname.startsWith('/api/') && r.method() !== 'GET') writes.push(`${r.method()} ${r.url()}`);
    });
    await mockApi(page, EMPTY);
    await gotoDashboard(page);

    const input = panel(page).getByLabel('Command to capture');
    await input.focus();
    await input.fill('python -m my_agent --task demo');
    await panel(page).getByRole('radio', { name: 'standard (default)' }).focus();
    await page.keyboard.press('ArrowRight');
    await expect(panel(page).getByRole('radio', { name: 'forensic' })).toBeChecked();
    await expect(page.getByTestId('onboarding-command')).toHaveText(
      `NOVA_CAPTURE_LEVEL=forensic nova capture --output-dir ${RUNS_LIST.capsule_dir} -- python -m my_agent --task demo`,
    );

    const copy = panel(page).getByRole('button', { name: 'Copy capture command' });
    await copy.focus();
    await page.keyboard.press('Enter');
    // Headless clipboards vary; either outcome is announced, and nothing runs.
    await expect(panel(page).getByRole('status').first()).toHaveText(/Command copied|Clipboard unavailable/);

    await panel(page).getByRole('button', { name: 'Check for runs' }).click();
    await expect(panel(page).getByText(`No runs yet in ${RUNS_LIST.capsule_dir}.`)).toBeVisible();
    expect(writes).toEqual([]);
  });

  test('refuses shell syntax instead of rewriting it', async ({ page }) => {
    await mockApi(page, EMPTY);
    await gotoDashboard(page);

    await panel(page).getByLabel('Command to capture').fill('python a.py | tee log');
    await expect(page.getByTestId('onboarding-command')).toHaveCount(0);
    await expect(panel(page).getByText(/shell syntax/)).toBeVisible();
    await expect(panel(page).getByRole('button', { name: 'Copy capture command' })).toBeDisabled();
  });

  test('is not shown when runs exist', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page);
    await expect(page.getByText('Recent Activity')).toBeVisible();
    await expect(panel(page)).toHaveCount(0);
  });

  test('is not shown when the run count is refused (unknown is not zero)', async ({ page }) => {
    await mockApi(page, {
      ...EMPTY,
      '/api/stats': { body: { ...STATS, run_count: null, failed_run_count: null, passed_run_count: null } },
    });
    await gotoDashboard(page);
    await expect(page.getByText('Recent Activity')).toBeVisible();
    await expect(panel(page)).toHaveCount(0);
  });

  test('fits a 390px viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockApi(page, EMPTY);
    await gotoDashboard(page);
    const box = await page.getByTestId('onboarding-command').boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x + box!.width).toBeLessThanOrEqual(391);
  });

  test('has no serious or critical axe violations', async ({ page }) => {
    await mockApi(page, EMPTY);
    await gotoDashboard(page);
    await expect(panel(page)).toBeVisible();
    const results = await new AxeBuilder({ page }).include('[data-testid="onboarding-panel"]').analyze();
    const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
    expect(serious, JSON.stringify(serious, null, 2)).toEqual([]);
  });
});
