/**
 * Accessibility checks for the authenticated embedded dashboard (`/dashboard`),
 * not only the showcase pages that `a11y.spec.ts` covers (#280).
 *
 * The API is served from fixtures (`fixtures/dashboard.ts`), so no `nova serve`
 * process is needed. axe runs with NO rule suppressions here except on the
 * Lineage view, where React Flow's generated markup trips the same two rules
 * `a11y.spec.ts` documents. A violation is a finding to fix, not to disable.
 */
import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mockApi, gotoDashboard, tabButton } from './fixtures/dashboard';

async function seriousViolations(page: Page, disable: string[] = []) {
  const results = await new AxeBuilder({ page }).disableRules(disable).analyze();
  const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  if (serious.length > 0) console.log('axe violations:', JSON.stringify(serious, null, 2));
  return serious;
}

const VIEWS: Array<{ tab: string; disable?: string[] }> = [
  { tab: 'home' },
  { tab: 'runs' },
  { tab: 'evidence' },
  { tab: 'compliance' },
  { tab: 'diff' },
  { tab: 'audit' },
  // Same React Flow exemptions as a11y.spec.ts, scoped to the one view that renders it.
  { tab: 'lineage', disable: ['duplicate-id-active', 'aria-prohibited-attr'] },
];

for (const { tab, disable } of VIEWS) {
  test(`a11y: /dashboard ?tab=${tab} has no serious or critical axe violations`, async ({ page }) => {
    await mockApi(page);
    await gotoDashboard(page, tab === 'home' ? '' : `tab=${tab}`);
    await page.waitForLoadState('networkidle');
    expect(await seriousViolations(page, disable)).toEqual([]);
  });
}

test('the shell has one h1 naming the current view, a main landmark and a skip link', async ({ page }) => {
  await mockApi(page);
  await gotoDashboard(page, 'tab=runs');

  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Runs');
  await expect(page.getByRole('main')).toBeVisible();

  // First Tab from the top of the page lands on the skip link; activating it moves focus to main.
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', { name: 'Skip to main content' });
  await expect(skip).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('main')).toBeFocused();
});

test('command palette: combobox semantics, focus stays inside, focus returns on close', async ({ page }) => {
  await mockApi(page);
  await gotoDashboard(page);

  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  const input = palette.getByRole('combobox', { name: 'Search commands' });
  await expect(input).toBeFocused();
  await expect(palette.getByRole('listbox')).toBeVisible();

  const firstId = await palette.getByRole('option').first().getAttribute('id');
  await expect(input).toHaveAttribute('aria-activedescendant', firstId ?? '');
  await page.keyboard.press('ArrowDown');
  await expect(palette.getByRole('option').nth(1)).toHaveAttribute('aria-selected', 'true');

  // Tab must not leave the modal.
  await page.keyboard.press('Tab');
  await expect(input).toBeFocused();

  expect(await seriousViolations(page)).toEqual([]);

  await page.keyboard.press('Escape');
  await expect(palette).toBeHidden();
});

test('keyboard help dialog takes focus and closes with Escape', async ({ page }) => {
  await mockApi(page);
  await gotoDashboard(page);

  await page.keyboard.press('?');
  const help = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
  await expect(help).toBeVisible();
  await expect(help.getByRole('button', { name: 'Close' })).toBeFocused();
  expect(await seriousViolations(page)).toEqual([]);

  await page.keyboard.press('Escape');
  await expect(help).toBeHidden();
});

test('single-key shortcuts can be turned off (WCAG 2.1.4)', async ({ page }) => {
  await mockApi(page);
  await gotoDashboard(page);

  await page.getByRole('button', { name: 'Appearance' }).click();
  await page.getByRole('checkbox', { name: /Single-key shortcuts/ }).uncheck();
  await page.keyboard.press('Escape');

  await page.keyboard.press('g');
  await page.keyboard.press('c');
  await expect(tabButton(page, 'Home')).toHaveAttribute('aria-current', 'page');

  // Ctrl+K is not a single-key shortcut and keeps working.
  await page.keyboard.press('Control+k');
  await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeVisible();
});

test('reduced motion: the dashboard renders with prefers-reduced-motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await mockApi(page);
  await gotoDashboard(page, 'tab=runs');
  // The global CSS rule collapses transitions to ~0ms.
  const duration = await page.getByRole('navigation').evaluate((el) => {
    const btn = el.querySelector('button');
    return btn ? getComputedStyle(btn).transitionDuration : '0s';
  });
  expect(duration.split(',').every((d) => parseFloat(d) < 0.01)).toBe(true);
});
