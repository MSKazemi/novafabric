import { test, expect } from '@playwright/test';

const DASHBOARD = '/dashboard';

// NOTE: The /dashboard route requires a live `nova serve` backend for the Sidebar,
// HomeTab, CommandsTab, and all other tabs to render. Without auth, only ConnectPanel
// is shown. Tests for sidebar journey-group labels, Commands tab interaction, live
// preview updates, and builder reset are intentionally omitted here — they require
// `nova serve --experimental` to be running with a valid token.

test('dashboard: page loads without JS errors', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  await page.goto(DASHBOARD);
  await page.waitForLoadState('networkidle');
  // Errors from failed API calls are expected when not connected — filter those
  const jsErrors = errors.filter(
    (e) => !e.includes('fetch') && !e.includes('Failed to load') && !e.includes('network'),
  );
  expect(jsErrors).toEqual([]);
});

test('dashboard: ConnectPanel is shown when not connected', async ({ page }) => {
  await page.goto(DASHBOARD);
  await page.waitForLoadState('networkidle');
  await expect(page.getByPlaceholder('http://127.0.0.1:4444')).toBeVisible();
});

test('dashboard: ConnectPanel shows nova serve connection form', async ({ page }) => {
  await page.goto(DASHBOARD);
  await page.waitForLoadState('networkidle');
  // ConnectPanel renders a login form — verify the URL input, token input, and submit button.
  // (The heading text includes a <code> element, matched via the URL placeholder instead.)
  await expect(page.getByPlaceholder('http://127.0.0.1:4444')).toBeVisible();
  await expect(page.getByPlaceholder('paste token from terminal')).toBeVisible();
  await expect(page.getByRole('button', { name: /Connect/i })).toBeVisible();
  // Verify NovaFabric branding text is present
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});

test('dashboard: ConnectPanel submit is disabled without input', async ({ page }) => {
  await page.goto(DASHBOARD);
  await page.waitForLoadState('networkidle');
  // The Connect button should be disabled when token/base fields are empty or token is blank
  const connectBtn = page.getByRole('button', { name: /Connect/i });
  await expect(connectBtn).toBeDisabled();
});

test('dashboard: ConnectPanel token field accepts input', async ({ page }) => {
  await page.goto(DASHBOARD);
  await page.waitForLoadState('networkidle');
  const tokenInput = page.getByPlaceholder('paste token from terminal');
  await tokenInput.fill('test-token-value');
  await expect(tokenInput).toHaveValue('test-token-value');
  // With a token filled in, the Connect button should become enabled
  // Button enable depends on both URL (defaults to 'http://127.0.0.1:4444') and token being non-empty
  await expect(page.getByRole('button', { name: /connect/i })).not.toBeDisabled();
});
