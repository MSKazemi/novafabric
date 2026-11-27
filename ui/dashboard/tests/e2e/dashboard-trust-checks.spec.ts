/**
 * Runs → Integrity: the per-check verification list (issue #271).
 *
 * A run's Integrity view verifies through POST /api/runs/{id}/verify and lists
 * every check `nova verify` performs, each with its own status in words and an
 * offline `nova verify` command. A self-asserted signer and an unsealed capsule
 * must never read as passed.
 */
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { gotoDashboard, mockApi } from './fixtures/dashboard';

const RUN = 'run-001';
const capsule = {
  run_id: RUN, capsule_path: `/capsules/${RUN}`, manifest: { run_id: RUN },
  trace: [], model_calls: [], tool_calls: [], lineage: [], assets: [], inputs: [], outputs: [],
};

/** What the server returns for a capsule verified from its own files (no novaseal.yaml). */
const SELF_ASSERTED = {
  sealed: true, configured: false, capsule_id: 'c0ffee', valid: true, errors: [],
  signature_ok: true, pae_encoding: 'dsse-v1', signing_intent: null, signing_intent_status: 'signed',
  timestamp_ok: null, timestamp_present: false, timestamp_strict: false, timestamp_trust: 'absent',
  log_integrity_ok: true, log_inclusion: 'carried-proof', sealer_log: 'no-log', log_notes: [],
  unanchored_claims: ['identity-self-asserted', 'log-head-unanchored'],
  capsule_binding_ok: true, capsule_id_ok: true, manifest_file: 'capsule.yaml',
  manifest_present: true, manifest_ok: true, unsealed_manifests: [], unsealed_refs: [],
  evidence_digests_present: true, evidence_digests_ok: true, binding_errors: [],
  identity_trust: 'self-asserted', identity_statement: 'proves the holder of this key signed',
  signer_subject: 'CN=sealer', local_seal_identity: true,
  ca_chain_checked: false, ca_chain_ok: null, ca_chain_reason: null,
  tsa_chain_checked: false, tsa_chain_ok: null, tsa_chain_reason: null,
  capture_tampered: null, special_entries: [],
};

test('Integrity lists every nova verify check, and a self-asserted signer is not established', async ({ page }) => {
  await mockApi(page, {
    [`/api/runs/${RUN}`]: { body: capsule },
    [`/api/runs/${RUN}/verify`]: { body: SELF_ASSERTED },
  });
  await gotoDashboard(page, `tab=runs&run=${RUN}&view=integrity`);
  await page.getByRole('button', { name: 'Verify seal' }).click();

  const list = page.getByRole('list', { name: 'Verification checks' });
  await expect(list.getByRole('article')).toHaveCount(11);
  await expect(page.getByRole('article', { name: 'Signature (DSSE ECDSA P-256)' })).toContainText('Passed');
  const signer = page.getByRole('article', { name: 'Signer identity (CA trust anchor)' });
  await expect(signer).toContainText('Not established');
  await expect(signer).toContainText(`nova verify --json --ca-bundle <ca-bundle.pem> /capsules/${RUN}`);
  await expect(page.getByRole('article', { name: 'Trusted time (TSA trust anchor)' })).toContainText('Not applicable');
  await expect(page.getByTestId('trust-verdict')).toContainText('Partially established');
  await expect(page.getByTestId('trust-verdict')).not.toContainText('Every check passed');

  // Structure only: lists, names and ARIA of the new section (contrast is the theme's).
  const axe = await new AxeBuilder({ page })
    .include('[data-testid="trust-checks"]')
    .disableRules(['color-contrast'])
    .analyze();
  expect(axe.violations.filter(v => v.impact === 'serious' || v.impact === 'critical')).toEqual([]);
});

test('an unsealed capsule shows no passed check', async ({ page }) => {
  await mockApi(page, {
    [`/api/runs/${RUN}`]: { body: capsule },
    [`/api/runs/${RUN}/verify`]: {
      body: { sealed: false, configured: null, message: 'No .seal/ directory — capsule was not sealed with NovaSeal.' },
    },
  });
  await gotoDashboard(page, `tab=runs&run=${RUN}&view=integrity`);
  await page.getByRole('button', { name: 'Verify seal' }).click();

  const list = page.getByRole('list', { name: 'Verification checks' });
  await expect(list.getByRole('article')).toHaveCount(11);
  await expect(list.locator('[data-status="passed"]')).toHaveCount(0);
  await expect(list.locator('[data-status="not_established"]')).toHaveCount(11);
  await expect(page.getByTestId('trust-verdict')).toContainText('Not sealed');
});
