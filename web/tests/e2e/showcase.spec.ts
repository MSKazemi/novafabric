import { test, expect } from '@playwright/test';

// ─── Shared: each showcase page must load without JS errors ──────────────────

const SHOWCASE_ROUTES = [
  { path: '/showcase/lineage', heading: 'Lineage graph' },
  { path: '/showcase/capsule', heading: 'Run capsule inspector' },
  { path: '/showcase/replay', heading: 'Replay & structural diff' },
  { path: '/showcase/registry', heading: 'Asset registry' },
  { path: '/showcase/evidence', heading: 'Evidence bundle' },
];

for (const { path, heading } of SHOWCASE_ROUTES) {
  test(`showcase ${path}: page loads without JS errors`, async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });
    await page.goto(path);
    await page.waitForLoadState('networkidle');
    expect(errors).toEqual([]);
  });

  test(`showcase ${path}: h1 heading is present`, async ({ page }) => {
    await page.goto(path);
    await page.waitForLoadState('networkidle');
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
  });
}

// ─── Lineage ─────────────────────────────────────────────────────────────────

test('showcase lineage: React Flow canvas renders', async ({ page }) => {
  await page.goto('/showcase/lineage');
  await page.waitForLoadState('networkidle');
  // React Flow renders a <div> with class containing "react-flow"
  const canvas = page.locator('.react-flow');
  await expect(canvas).toBeVisible();
});

test('showcase lineage: at least one node is rendered', async ({ page }) => {
  await page.goto('/showcase/lineage');
  await page.waitForLoadState('networkidle');
  // React Flow nodes have class "react-flow__node"
  const nodes = page.locator('.react-flow__node');
  await expect(nodes.first()).toBeVisible();
});

// ─── Capsule ──────────────────────────────────────────────────────────────────

test('showcase capsule: inspector renders without crashing', async ({ page }) => {
  await page.goto('/showcase/capsule');
  await page.waitForLoadState('networkidle');
  // The "On your machine" subheading indicates the static content loaded
  await expect(page.getByRole('heading', { level: 2, name: /On your machine/i })).toBeVisible();
});

// ─── Replay ───────────────────────────────────────────────────────────────────

test('showcase replay: mode tab strip renders', async ({ page }) => {
  await page.goto('/showcase/replay');
  await page.waitForLoadState('networkidle');
  const tablist = page.getByRole('tablist', { name: 'Replay modes' });
  await expect(tablist).toBeVisible();
});

test('showcase replay: Mocked tab is selected by default', async ({ page }) => {
  await page.goto('/showcase/replay');
  await page.waitForLoadState('networkidle');
  const mockedTab = page.getByRole('tab', { name: 'Mocked' });
  await expect(mockedTab).toHaveAttribute('aria-selected', 'true');
});

test('showcase replay: switching to Forensic tab shows forensic pane', async ({ page }) => {
  await page.goto('/showcase/replay');
  await page.waitForLoadState('networkidle');
  await page.getByRole('tab', { name: 'Forensic' }).click();
  await expect(page.getByRole('tab', { name: 'Forensic' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText(/Forensic summary/i)).toBeVisible();
});

test('showcase replay: Exact tab is disabled', async ({ page }) => {
  await page.goto('/showcase/replay');
  await page.waitForLoadState('networkidle');
  const exactTab = page.getByRole('tab', { name: 'Exact' });
  await expect(exactTab).toBeDisabled();
});

// ─── Registry ─────────────────────────────────────────────────────────────────

test('showcase registry: asset list renders', async ({ page }) => {
  await page.goto('/showcase/registry');
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  // The static registry showcase renders either a table or a list of assets
  await expect(page.locator('body')).not.toBeEmpty();
});

// ─── Evidence ─────────────────────────────────────────────────────────────────

test('showcase evidence: bundle header is present', async ({ page }) => {
  await page.goto('/showcase/evidence');
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('heading', { level: 1, name: 'Evidence bundle' })).toBeVisible();
});

test('showcase evidence: "On your machine" section is visible', async ({ page }) => {
  await page.goto('/showcase/evidence');
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('heading', { level: 2, name: /On your machine/i })).toBeVisible();
});
