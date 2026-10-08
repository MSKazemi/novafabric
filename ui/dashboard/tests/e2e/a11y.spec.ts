import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const ROUTES = [
  '/',
  '/concepts',
  '/install',
  '/why',
  '/spec',
  '/showcase/lineage',
  '/showcase/capsule',
  '/showcase/replay',
  '/showcase/registry',
  '/showcase/evidence',
];

for (const path of ROUTES) {
  test(`a11y: ${path} has no serious or critical violations`, async ({ page }) => {
    await page.goto(path);
    // Allow time for React islands to hydrate.
    await page.waitForLoadState('networkidle');

    const results = await new AxeBuilder({ page })
      .disableRules([
        // React Flow's nodes use generated ids that axe flags as "duplicate-id-active";
        // these are component-internal and not user-visible duplicates.
        'duplicate-id-active',
        // React Flow uses aria-label on <span> elements that don't have an explicit
        // role — the attribute is added internally by the library and cannot be
        // removed without forking or patching React Flow.
        'aria-prohibited-attr',
      ])
      .analyze();

    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    if (serious.length > 0) {
      console.log('axe violations:', JSON.stringify(serious, null, 2));
    }
    expect(serious).toEqual([]);
  });
}
