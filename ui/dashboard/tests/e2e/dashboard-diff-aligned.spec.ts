/**
 * Issue #270 (first slice) — aligned A/B comparison in the Diff tab.
 *
 * A deep link with two run ids auto-runs the diff; the aligned view lists the
 * server's model/tool-call pairs with verdicts in words, highlights the first
 * divergent pair, reaches it from the keyboard, and shows the equivalent
 * `nova diff` command.
 */
import { test, expect } from '@playwright/test';
import { mockApi, gotoDashboard } from './fixtures/dashboard';

const ZERO = { a: { model_calls: 0, tool_calls: 0 }, b: { model_calls: 0, tool_calls: 0 } };

const DIFF = {
  run_a_id: 'run-001',
  run_b_id: 'run-002',
  report: {
    run_a_id: 'run-001',
    run_b_id: 'run-002',
    env_changes: [],
    model_call_pairs: [
      { changed: false, model_call_id_a: 'MC-1', model_call_id_b: 'MC-1' },
      { changed: true, response_changed: true, model_call_id_a: 'MC-2', model_call_id_b: 'MC-2' },
      { added: true, model_call_id_b: 'MC-3' },
    ],
    tool_call_pairs: [{ removed: true, tool_name: 'fetch', tool_call_id_a: 'TC-1' }],
    output_changes: [],
    skipped_malformed_lines: ZERO,
    first_divergence: {
      index: 1, index_a: 1, index_b: 1, stream: 'model_calls', event_type: 'model_call',
      reason: 'changed', aspects: ['response'], record_id_a: 'MC-2', record_id_b: 'MC-2',
      timeline: 'per_stream',
    },
  },
  summary: { changed: 1, added: 1, removed: 1 },
  sections: { model_calls: { aligned: 2, changed: 1, added: 1, removed: 0 } },
  has_changes: true,
  skipped_malformed_lines: ZERO,
};

test('a two-run deep link shows the aligned comparison with the first divergence', async ({ page }) => {
  await mockApi(page, { '/api/diff': { body: DIFF } });
  await gotoDashboard(page, 'run_a=run-001&run_b=run-002');

  const view = page.getByTestId('aligned-diff');
  await expect(view.getByRole('heading', { name: 'Aligned comparison' })).toBeVisible();
  await expect(view.getByText('Step 1: model call changed (response)')).toBeVisible();

  const models = view.getByRole('table', { name: /Model calls/ });
  const first = models.locator('tr[aria-current="step"]');
  await expect(first).toHaveCount(1);
  await expect(first).toContainText('First divergence');
  await expect(first).toContainText('MC-2');
  await expect(models.getByText('Added in B')).toBeVisible();
  await expect(view.getByRole('table', { name: /Tool calls/ }).getByText('Removed in B')).toBeVisible();

  // Keyboard: the jump button moves focus onto the highlighted row.
  const jump = view.getByRole('button', { name: 'Go to first divergence' });
  await jump.focus();
  await page.keyboard.press('Enter');
  await expect(first).toBeFocused();

  await expect(view.getByText('nova diff run-001 run-002', { exact: true })).toBeVisible();
  await expect(view.getByRole('button', { name: 'Copy CLI command: nova diff run-001 run-002' })).toBeVisible();
});

test('an older server without first_divergence shows "not derivable", never identical', async ({ page }) => {
  const { first_divergence: _fd, ...report } = DIFF.report;
  await mockApi(page, { '/api/diff': { body: { ...DIFF, report } } });
  await gotoDashboard(page, 'run_a=run-001&run_b=run-002');

  const view = page.getByTestId('aligned-diff');
  await expect(view.getByText(/Not derivable\./)).toBeVisible();
  await expect(view.getByText('The runs differ.')).toBeVisible();
  await expect(view.getByText(/No differences/)).toHaveCount(0);
});
