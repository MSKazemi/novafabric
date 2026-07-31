/**
 * Authenticated dashboard shell — boot, navigation, keyboard, command palette.
 *
 * Everything behind the token gate used to be untested end-to-end (see the
 * note at the top of dashboard-v08.spec.ts). These specs close that gap
 * without a `nova serve` process: the API is served from fixtures by
 * `tests/e2e/fixtures/dashboard.ts` and the session token is injected the way
 * a real operator gets it — `?token=` in the URL, consumed by
 * `DashboardApp.consumeTokenFromUrl` into localStorage.
 */
import { test, expect } from '@playwright/test';
import {
  TOKEN,
  mockApi,
  gotoDashboard,
  nav,
  revealTab,
  tabButton,
  groupHeader,
  urlParam,
} from './fixtures/dashboard';

/** The 7 information-architecture groups declared in Sidebar.NAV_GROUPS. */
const NAV_GROUP_LABELS = [
  'Overview',
  'Runs & Debug',
  'Govern & Promote',
  'Provenance & Trust',
  'Compliance',
  'Platform',
  'Reports & Export',
];

test.describe('boot (journey 1)', () => {
  test('?token= in the URL renders the authenticated shell, not ConnectPanel', async ({ page }) => {
    await mockApi(page);
    await page.goto(`/dashboard?token=${TOKEN}`);

    await expect(nav(page)).toBeVisible();
    await expect(tabButton(page, 'Home')).toHaveAttribute('aria-current', 'page');
    // ConnectPanel's token field must be gone once connected.
    await expect(page.getByPlaceholder('paste token from terminal')).toHaveCount(0);
  });

  test('the token is stashed in localStorage and scrubbed from the URL', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page);

    const stored = await page.evaluate(() => ({
      token: localStorage.getItem('novafabric.serve-token'),
      base: localStorage.getItem('novafabric.serve-base'),
    }));
    expect(stored.token).toBe(TOKEN);
    expect(stored.base).toBe(new URL(page.url()).origin);
    // consumeTokenFromUrl() rewrites history so the token is not left in the bar.
    expect(page.url()).not.toContain('token=');
  });

  test('a rejected token falls back to ConnectPanel with an explanation', async ({ page }) => {
    // validateToken() probes /api/runs; a 401 there is the "wrong token" path.
    await mockApi(page, { '/api/runs': { status: 401, body: { detail: 'unauthorized' } } });
    await page.goto(`/dashboard?token=${TOKEN}`);

    await expect(page.getByPlaceholder('paste token from terminal')).toBeVisible();
    await expect(page.getByText(/Token rejected/i)).toBeVisible();
    await expect(nav(page)).toHaveCount(0);
  });
});

test.describe('navigation (journey 2)', () => {
  test('all 7 sidebar groups render', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page);

    for (const label of NAV_GROUP_LABELS) {
      await expect(groupHeader(page, label)).toBeVisible();
    }
  });

  test('clicking a tab switches the view and updates ?tab=', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page);

    // Groups collapse by default — reveal the one holding Registry first.
    await revealTab(page, 'Registry');
    await tabButton(page, 'Registry').click();
    await expect(tabButton(page, 'Registry')).toHaveAttribute('aria-current', 'page');
    // Overview collapses once the active tab moves out of it, so Home's button
    // leaves the DOM entirely — which is a stronger statement than "not current".
    await expect(tabButton(page, 'Home')).toHaveCount(0);
    // Breadcrumb in the top bar follows the active tab.
    await expect(page.getByRole('main').getByText('Registry', { exact: true }).first()).toBeVisible();
    await expect.poll(() => urlParam(page, 'tab')).toBe('registry');

    // Home is the default view, so it clears ?tab= instead of pinning it.
    await revealTab(page, 'Home');
    await tabButton(page, 'Home').click();
    await expect(tabButton(page, 'Home')).toHaveAttribute('aria-current', 'page');
    await expect.poll(() => urlParam(page, 'tab')).toBeNull();
  });

  test('a direct ?tab= deep link lands on that tab', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page, 'tab=lineage');

    await expect(tabButton(page, 'Lineage')).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('main').getByText('Lineage', { exact: true }).first()).toBeVisible();
    await expect.poll(() => urlParam(page, 'tab')).toBe('lineage');
  });

  test('an unknown ?tab= value falls back to Home', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page, 'tab=not-a-tab');

    await expect(tabButton(page, 'Home')).toHaveAttribute('aria-current', 'page');
  });
});

test.describe('g-key shortcuts (journey 3)', () => {
  test('g then the tab key navigates', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page);

    await page.keyboard.press('g');
    await page.keyboard.press('c');
    await expect(tabButton(page, 'Compliance')).toHaveAttribute('aria-current', 'page');

    await page.keyboard.press('g');
    await page.keyboard.press('r');
    await expect(tabButton(page, 'Runs')).toHaveAttribute('aria-current', 'page');
  });

  test('an unmapped second key after g does not navigate', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page, 'tab=policy');

    await page.keyboard.press('g');
    await page.keyboard.press('0'); // no tab claims '0'
    await expect(tabButton(page, 'Policy')).toHaveAttribute('aria-current', 'page');
  });

  test('a shortcut typed inside an input does NOT navigate', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page, 'tab=runs');

    const search = page.getByPlaceholder(/Search run_id/);
    await expect(search).toBeVisible();
    await search.click();
    // 'g' then 'c' would jump to Compliance from the document body.
    await search.pressSequentially('gc');

    await expect(search).toHaveValue('gc');
    await expect(tabButton(page, 'Runs')).toHaveAttribute('aria-current', 'page');
    // Compliance lives in a collapsed group after navigating to Runs, so the
    // button may not be in the DOM at all — either way it must not be current.
    await expect(tabButton(page, 'Compliance')).toHaveCount(0);
  });
});

test.describe('command palette (journey 5)', () => {
  test('Ctrl+K opens it, typing filters, Escape closes', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page);

    await page.keyboard.press('Control+k');
    const palette = page.getByRole('dialog', { name: 'Command palette' });
    await expect(palette).toBeVisible();

    // Unfiltered: every nav target is offered.
    await expect(palette.getByRole('button', { name: 'Lineage', exact: false }).first()).toBeVisible();
    await expect(palette.getByRole('button', { name: /^Home/ })).toHaveCount(1);

    const input = palette.getByRole('textbox', { name: 'Search commands' });
    await input.fill('lineage');
    await expect(palette.getByRole('button', { name: /Lineage/ }).first()).toBeVisible();
    await expect(palette.getByRole('button', { name: /^Home/ })).toHaveCount(0);

    await page.keyboard.press('Escape');
    await expect(palette).toBeHidden();
  });

  test('selecting a palette entry navigates', async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page);

    await page.keyboard.press('Control+k');
    const palette = page.getByRole('dialog', { name: 'Command palette' });
    await palette.getByRole('textbox', { name: 'Search commands' }).fill('lineage');
    await palette.getByRole('button', { name: /Lineage/ }).first().click();

    await expect(palette).toBeHidden();
    await expect(tabButton(page, 'Lineage')).toHaveAttribute('aria-current', 'page');
  });
});
