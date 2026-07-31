/**
 * Runs tab — truncation honesty (journey 6) and error resilience (journey 7).
 *
 * ADR-0199 makes silent truncation a defect class: a bounded list MUST tell
 * the user how much more exists server-side. The Runs tab is fed by
 * `/api/runs/search`, whose `total_approx` drives both affordances asserted
 * here (the list header counter and the shared `TruncationNotice` footer).
 */
import { test, expect } from '@playwright/test';
import {
  mockApi,
  gotoDashboard,
  makeRun,
  runSearchPage,
  tabButton,
  urlParam,
} from './fixtures/dashboard';

test.describe('truncation honesty — ADR-0199 (journey 6)', () => {
  test('a page far smaller than total_approx surfaces "Showing N of ~M"', async ({ page }) => {
    const items = [makeRun(1), makeRun(2), makeRun(3)];
    await mockApi(page, {
      '/api/runs/search': {
        body: runSearchPage({ items, nextCursor: 'cursor-page-2', totalApprox: 12480 }),
      },
    });
    await gotoDashboard(page, 'tab=runs');

    // Footer affordance (TruncationNotice) — count, approximate marker, remainder.
    const notice = page.getByText(/Showing 3 of ~12[.,\s]?480/);
    await expect(notice).toBeVisible();
    await expect(page.getByText(/12[.,\s]?477 more/)).toBeVisible();
    // …and an escape hatch to the rest of the data.
    await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible();

    // Header counter states the same truth next to the list title.
    await expect(page.getByText(/\(3 of ~12480\)/)).toBeVisible();
  });

  test('no truncation notice when the page is the whole result set', async ({ page }) => {
    const items = [makeRun(1), makeRun(2)];
    await mockApi(page, {
      '/api/runs/search': { body: runSearchPage({ items, nextCursor: null, totalApprox: 2 }) },
    });
    await gotoDashboard(page, 'tab=runs');

    await expect(page.getByText('python job_1.py')).toBeVisible();
    await expect(page.getByText(/Showing \d+ of/)).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0);
  });

  test('Load more appends the next cursor page and the counter follows', async ({ page }) => {
    // First call: page 1 with a cursor. Second call: page 2, no cursor.
    let call = 0;
    await mockApi(page);
    await page.route(
      (url) => url.pathname === '/api/runs/search',
      (route) => {
        call += 1;
        const body =
          call === 1
            ? runSearchPage({ items: [makeRun(1), makeRun(2)], nextCursor: 'c2', totalApprox: 4 })
            : runSearchPage({ items: [makeRun(3), makeRun(4)], nextCursor: null, totalApprox: 4 });
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      },
    );
    await gotoDashboard(page, 'tab=runs');

    await expect(page.getByText(/Showing 2 of ~4/)).toBeVisible();
    await expect(page.getByText('python job_4.py')).toHaveCount(0);

    await page.getByRole('button', { name: 'Load more' }).click();

    // Page 2 appended, and the notice retires once nothing is left server-side.
    await expect(page.getByText('python job_4.py')).toBeVisible();
    await expect(page.getByText(/Showing \d+ of/)).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0);
  });
});

test.describe('error resilience (journey 7)', () => {
  test('a 500 from the tab API shows an error state, and the user can navigate away', async ({ page }) => {
    await mockApi(page, {
      '/api/runs/search': { status: 500, body: { detail: 'index unavailable' } },
    });
    await gotoDashboard(page, 'tab=runs');

    // Failure is surfaced, not swallowed — with a retry affordance.
    await expect(page.getByText(/Error: index unavailable/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
    // The shell itself survives: sidebar + breadcrumb still there.
    await expect(tabButton(page, 'Runs')).toHaveAttribute('aria-current', 'page');

    // The per-tab boundary is keyed on `tab`, so switching away remounts clean.
    await tabButton(page, 'Registry').click();
    await expect(tabButton(page, 'Registry')).toHaveAttribute('aria-current', 'page');
    await expect(page.getByText(/Error: index unavailable/)).toHaveCount(0);
    await expect.poll(() => urlParam(page, 'tab')).toBe('registry');

    // …and back again: the broken tab still renders its error, not a blank pane.
    await tabButton(page, 'Runs').click();
    await expect(page.getByText(/Error: index unavailable/)).toBeVisible();
  });

  test('a 500 on a secondary endpoint does not take the tab down', async ({ page }) => {
    // Sidebar counts are best-effort: /api/stats failing must not break boot.
    await mockApi(page, { '/api/stats': { status: 500, body: { detail: 'stats offline' } } });
    await gotoDashboard(page, 'tab=runs');

    await expect(page.getByText('python job_1.py')).toBeVisible();
    await expect(tabButton(page, 'Runs')).toHaveAttribute('aria-current', 'page');
  });
});
