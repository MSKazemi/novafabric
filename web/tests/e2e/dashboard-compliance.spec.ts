/**
 * Compliance hub (journey 4) — the `?sub=` sub-navigation over the 22-panel
 * manifest in `tabs/compliance/`.
 *
 * Invariants locked down here:
 *   - the SegmentedControl switches groups and swaps the rendered panels;
 *   - the active group is deep-linkable via `?sub=`;
 *   - the default group keeps the URL clean (no `?sub=frameworks`);
 *   - an unknown `?sub=` value degrades to the default group instead of
 *     rendering an empty hub.
 */
import { test, expect } from '@playwright/test';
import { mockApi, gotoDashboard, tabButton, urlParam } from './fixtures/dashboard';

// Segment accessible names are the label immediately followed by the panel
// count (e.g. "Frameworks4"), so match on the label prefix only.
const segment = (page: import('@playwright/test').Page, label: string) =>
  page.getByRole('tab', { name: new RegExp(`^${label}`) });

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test('defaults to the Frameworks group with no ?sub= in the URL', async ({ page }) => {
  await gotoDashboard(page, 'tab=compliance');

  await expect(tabButton(page, 'Compliance')).toHaveAttribute('aria-current', 'page');
  await expect(segment(page, 'Frameworks')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', { name: 'EU AI Act Annex IV' })).toBeVisible();
  expect(urlParam(page, 'sub')).toBeNull();
});

test('the SegmentedControl switches sub-groups and updates ?sub=', async ({ page }) => {
  await gotoDashboard(page, 'tab=compliance');

  await segment(page, 'Privacy').click();

  await expect(segment(page, 'Privacy')).toHaveAttribute('aria-selected', 'true');
  await expect(segment(page, 'Frameworks')).toHaveAttribute('aria-selected', 'false');
  // Panels swapped: a privacy panel is in, the frameworks panel is out.
  await expect(page.getByRole('heading', { name: 'GDPR Subject Proof' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'EU AI Act Annex IV' })).toHaveCount(0);
  await expect.poll(() => urlParam(page, 'sub')).toBe('privacy');

  // Returning to the default group drops the param again (clean URLs).
  await segment(page, 'Frameworks').click();
  await expect(page.getByRole('heading', { name: 'EU AI Act Annex IV' })).toBeVisible();
  await expect.poll(() => urlParam(page, 'sub')).toBeNull();
});

test('a direct ?tab=compliance&sub=privacy deep link lands on Privacy', async ({ page }) => {
  await gotoDashboard(page, 'tab=compliance&sub=privacy');

  await expect(segment(page, 'Privacy')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', { name: 'GDPR Subject Proof' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'EU AI Act Annex IV' })).toHaveCount(0);
});

test('an unknown ?sub= value falls back to the default group', async ({ page }) => {
  await gotoDashboard(page, 'tab=compliance&sub=nonsense');

  await expect(segment(page, 'Frameworks')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', { name: 'EU AI Act Annex IV' })).toBeVisible();
});

test('every declared compliance group is reachable from the hub', async ({ page }) => {
  await gotoDashboard(page, 'tab=compliance');

  for (const [label, sub, panel] of [
    ['Audits', 'audits', 'Compliance Audit'],
    ['Exports', 'exports', 'Examiner Export'],
    ['Assurance', 'assurance', 'Tool Permission Events'],
  ] as const) {
    await segment(page, label).click();
    await expect(segment(page, label)).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('heading', { name: panel })).toBeVisible();
    await expect.poll(() => urlParam(page, 'sub')).toBe(sub);
  }
});
