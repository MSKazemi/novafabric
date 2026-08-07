/**
 * Sidebar layout: the left panel must be wide enough for its own content, and
 * the hover shortcut hint must not paint on top of it.
 *
 * jsdom cannot catch either of these — it has no layout engine, so `scrollWidth`
 * and `getBoundingClientRect()` are meaningless there. Both regressions were
 * found by measuring in a real browser and are pinned here the same way.
 *
 * The bug: the `g x` shortcut hint is absolutely positioned at `right-2` and
 * fades in on hover, while the row's badge/count sits in flow at that same
 * right edge. Measured on the deployed v0.101.0 build: 14px of overlap on all
 * 36 rows, hint at 60% opacity over the badge — text on text.
 */
import { test, expect } from '@playwright/test';
import { TOKEN, mockApi, nav } from './fixtures/dashboard';

test.describe('sidebar layout', () => {
  test('the expanded sidebar is wide enough for its content', async ({ page }) => {
    await mockApi(page);
    await page.goto(`/dashboard?token=${TOKEN}`);
    await expect(nav(page)).toBeVisible();

    const aside = page.locator('aside[aria-label="Dashboard navigation"]');
    const width = await aside.evaluate((el) => el.getBoundingClientRect().width);

    // w-60 = 15rem = 240px. The old w-52 (208px) left no gap between a row's
    // badge and the hover hint.
    expect(width).toBeGreaterThanOrEqual(240);
  });

  test('no sidebar text is truncated at the default width', async ({ page }) => {
    await mockApi(page);
    await page.goto(`/dashboard?token=${TOKEN}`);
    await expect(nav(page)).toBeVisible();

    const aside = page.locator('aside[aria-label="Dashboard navigation"]');
    // Expand every group so all labels are laid out.
    const headers = aside.locator('nav > div > button:first-child');
    for (let i = 0; i < (await headers.count()); i++) {
      const h = headers.nth(i);
      if ((await h.getAttribute('aria-expanded')) === 'false') await h.click();
    }

    const clipped = await aside.evaluate((el) =>
      [...el.querySelectorAll<HTMLElement>('span, code')]
        .filter((c) => c.children.length === 0 && (c.textContent ?? '').trim())
        .filter((c) => c.scrollWidth - c.clientWidth > 1)
        .map((c) => ({ text: (c.textContent ?? '').trim(), over: c.scrollWidth - c.clientWidth })),
    );

    expect(clipped).toEqual([]);
  });

  test('the hover shortcut hint never overlaps visible row content', async ({ page }) => {
    await mockApi(page);
    await page.goto(`/dashboard?token=${TOKEN}`);
    await expect(nav(page)).toBeVisible();

    const aside = page.locator('aside[aria-label="Dashboard navigation"]');
    const headers = aside.locator('nav > div > button:first-child');
    for (let i = 0; i < (await headers.count()); i++) {
      const h = headers.nth(i);
      if ((await h.getAttribute('aria-expanded')) === 'false') await h.click();
    }

    const rows = aside.locator('nav button[title]'); // tab rows carry a title; headers do not
    const total = await rows.count();
    expect(total).toBeGreaterThan(10);

    const collisions: unknown[] = [];
    for (let i = 0; i < total; i++) {
      const row = rows.nth(i);
      await row.hover();
      const hit = await row.evaluate((btn) => {
        const kids = [...btn.children] as HTMLElement[];
        const hint = kids.find(
          (c) => getComputedStyle(c).position === 'absolute' && /^g /.test((c.textContent ?? '').trim()),
        );
        if (!hint) return null;
        if (parseFloat(getComputedStyle(hint).opacity) < 0.01) return null; // not shown
        const a = hint.getBoundingClientRect();
        for (const c of kids) {
          if (c === hint) continue;
          const cs = getComputedStyle(c);
          if (cs.position === 'absolute') continue;
          if (parseFloat(cs.opacity) < 0.01) continue; // faded out on purpose
          if (!(c.textContent ?? '').trim()) continue;
          const b = c.getBoundingClientRect();
          // Compare against the painted glyphs, not the flex box: a `flex-1`
          // label spans the row even when its text is short.
          const r = document.createRange();
          r.selectNodeContents(c);
          const glyphs = r.getBoundingClientRect();
          r.detach();
          const box = glyphs.width > 0 ? glyphs : b;
          const overlap = Math.min(a.right, box.right) - Math.max(a.left, box.left);
          if (overlap > 0) {
            return { row: (btn.textContent ?? '').trim(), over: (c.textContent ?? '').trim(), overlap };
          }
        }
        return null;
      });
      if (hit) collisions.push(hit);
    }

    expect(collisions).toEqual([]);
  });

  test('the auto-opened group closes again when you navigate out of it', async ({ page }) => {
    // Keyboard navigation only — no group header is ever clicked, so Overview
    // stays under the sidebar's control and must tidy itself away. This is the
    // property dashboard-shell.spec.ts used to assert before `revealTab`'s
    // header-clicking made that test's Overview user-owned.
    await mockApi(page);
    await page.goto(`/dashboard?token=${TOKEN}`);
    await expect(nav(page)).toBeVisible();

    const aside = page.locator('aside[aria-label="Dashboard navigation"]');
    await expect(aside.getByRole('button', { name: 'Home' })).toBeVisible();

    // `g` then `g` → Registry, which lives in 'Govern & Promote'.
    await page.keyboard.press('g');
    await page.keyboard.press('g');

    await expect(aside.getByRole('button', { name: 'Registry' })).toHaveAttribute('aria-current', 'page');
    // Overview released: its rows leave the DOM entirely.
    await expect(aside.getByRole('button', { name: 'Home' })).toHaveCount(0);
  });

  test('every group, including the one holding the active tab, can be collapsed', async ({ page }) => {
    await mockApi(page);
    await page.goto(`/dashboard?token=${TOKEN}`);
    await expect(nav(page)).toBeVisible();

    const aside = page.locator('aside[aria-label="Dashboard navigation"]');
    // 'Home' is active on boot and lives in Overview, which starts expanded.
    const overview = aside.locator('nav > div > button:first-child').first();
    await expect(overview).toHaveAttribute('aria-expanded', 'true');
    await expect(aside.getByRole('button', { name: 'Analytics' })).toBeVisible();

    await overview.click();

    await expect(overview).toHaveAttribute('aria-expanded', 'false');
    await expect(aside.getByRole('button', { name: 'Analytics' })).toHaveCount(0);
  });
});
