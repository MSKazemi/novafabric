/**
 * Issue #276: one click from an alert or incident opens a read-only triage panel that links
 * the exact run, says plainly when no run is linked, and only offers copyable commands.
 */
import { test, expect } from '@playwright/test';
import { gotoDashboard, INCIDENTS, mockApi } from './fixtures/dashboard';

const capsule = (id: string) => ({
  run_id: id, capsule_path: `/capsules/${id}`, manifest: { run_id: id, status: 'success' },
  trace: [], model_calls: [], tool_calls: [], lineage: [], assets: [], inputs: [], outputs: [],
});

const ALERTS = {
  total: 2,
  alerting_configured: true,
  alerts: [
    {
      id: 'EVT-SEAL', timestamp: '2026-11-20T10:00:00+00:00', event_type: 'ops.seal.verify_failed',
      severity: 'critical', subject: 'capsule:3f2a', outcome: 'delivered', endpoint_id: 'webhook-1',
      attempts: 1, run_id: 'run-001',
      first_divergence: { path: 'model-calls.jsonl', reason: 'modified', stream: 'model_calls', divergent_files: 1 },
    },
    {
      id: 'E-Q', timestamp: '2026-11-20T09:00:00+00:00', event_type: 'ops.quota.breached',
      severity: 'critical', subject: 'quota:capsules', outcome: 'emitted', endpoint_id: null,
      attempts: 0, run_id: null, first_divergence: null,
    },
  ],
};

test('alert triage links the recorded run and lists copyable commands; nothing is posted', async ({ page }) => {
  await mockApi(page, {
    '/api/alerts/recent': { body: ALERTS },
    '/api/runs/run-001': { body: capsule('run-001') },
  });
  await gotoDashboard(page, 'tab=alerts');
  // From here on, the triage journey must not send a single non-GET API request.
  const posts: string[] = [];
  page.on('request', (r) => {
    if (r.method() !== 'GET' && new URL(r.url()).pathname.startsWith('/api/')) posts.push(r.url());
  });

  await page.getByRole('button', { name: 'Triage alert ops.seal.verify_failed about capsule:3f2a' }).click();
  const panel = page.getByRole('dialog', { name: /Triage — ops.seal.verify_failed/ });
  await expect(panel).toBeVisible();
  await expect(panel.getByText('/capsules/run-001', { exact: true })).toBeVisible();
  await expect(panel.getByText('nova verify /capsules/run-001 --json')).toBeVisible();
  await expect(panel.getByText('model-calls.jsonl', { exact: true })).toBeVisible();
  await expect(panel.getByRole('link', { name: 'Open run run-001 in the Runs tab' })).toHaveAttribute(
    'href', /\?tab=runs&run=run-001$/,
  );

  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  expect(posts).toEqual([]);
});

test('an alert without a recorded run says so instead of guessing', async ({ page }) => {
  await mockApi(page, { '/api/alerts/recent': { body: ALERTS } });
  await gotoDashboard(page, 'tab=alerts');
  await page.getByRole('button', { name: 'Triage alert ops.quota.breached about quota:capsules' }).click();
  const panel = page.getByRole('dialog', { name: /Triage — ops.quota.breached/ });
  await expect(panel.getByText('No run linked — the alert source did not record a run id.')).toBeVisible();
  await expect(panel.getByRole('link', { name: /Open run/ })).toHaveCount(0);
});

test('incident triage opens from the row, and Open run lands on that run with Back returning', async ({ page }) => {
  await mockApi(page, {
    '/api/incidents': { body: INCIDENTS },
    '/api/runs/run-001': { body: capsule('run-001') },
  });
  await gotoDashboard(page, 'tab=incidents');
  await page.getByRole('button', { name: 'Triage incident Fixture incident' }).click();
  const panel = page.getByRole('dialog', { name: /Triage — Fixture incident/ });
  await expect(panel.getByText('nova incident status inc-001')).toBeVisible();
  await expect(panel.getByText('/capsules/run-001', { exact: true })).toBeVisible();

  await panel.getByRole('link', { name: 'Open run run-001 in the Runs tab' }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('run')).toBe('run-001');
  await page.goBack();
  await expect.poll(() => new URL(page.url()).searchParams.get('tab')).toBe('incidents');
});
