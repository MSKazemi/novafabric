/**
 * Trust Center per-check verification (issue #271).
 *
 * Acceptance criteria pinned here:
 * - Every check `nova verify <capsule-dir>` performs has its own row and status.
 * - A check reads "Passed" only when the server reported that it ran and passed:
 *   a missing field, an unsealed capsule or a missing key/anchor is
 *   "Not established", never "Passed".
 * - A valid signature from a self-asserted (untrusted) signer, an absent or
 *   unanchored timestamp, or a seal without evidence_digests never yields the
 *   "Every check passed" verdict.
 * - Tampered evidence, a failed chain, planted links or recorded capture
 *   tampering read "Failed".
 * - Each check carries an offline `nova verify` command with real flags and the
 *   capsule path shell-quoted.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { CapsuleVerifyResult } from '@/lib/api';
import {
  buildTrustChecks,
  shellQuote,
  trustSummary,
  trustVerdict,
  verifyCommands,
  type TrustCheck,
  type TrustStatus,
} from '@/components/dashboard/trust/checks';
import TrustChecks from '@/components/dashboard/trust/TrustChecks';
import { IntegrityPanel } from '@/components/dashboard/tabs/runs/ExplorerPanels';
import { GENERATED_COMMANDS } from '@/components/dashboard/commands/generatedCommands';

const CHECK_IDS = [
  'signature', 'timestamp-token', 'timestamp-trust', 'log-inclusion', 'sealer-log',
  'manifest-binding', 'evidence-binding', 'capsule-id', 'signer-identity',
  'capture-integrity', 'special-entries',
];

/** Every check ran and passed against anchors the verifier supplied. */
const ANCHORED: CapsuleVerifyResult = {
  sealed: true, configured: true, capsule_id: 'c0ffee', valid: true, errors: [],
  signature_ok: true, pae_encoding: 'dsse-v1', signing_intent: null, signing_intent_status: 'signed',
  timestamp_ok: true, timestamp_present: true, timestamp_strict: true, timestamp_trust: 'anchored',
  timestamp_gen_time: '2026-11-01T00:00:00Z', tsa_subject: 'CN=Example TSA',
  tsa_chain_checked: true, tsa_chain_ok: true, tsa_chain_reason: 'chain ok',
  log_integrity_ok: true, log_inclusion: 'local-log', sealer_log: 'entry-found', log_notes: [],
  unanchored_claims: [], capsule_binding_ok: true, capsule_id_ok: true,
  manifest_file: 'capsule.yaml', manifest_present: true, manifest_ok: true,
  unsealed_manifests: [], unsealed_refs: [],
  evidence_digests_present: true, evidence_digests_ok: true, binding_errors: [],
  identity_trust: 'ca-anchored', identity_statement: 'chains to an anchor you supplied',
  signer_subject: 'CN=sealer', local_seal_identity: false,
  ca_chain_checked: true, ca_chain_ok: true, ca_chain_reason: 'chain ok',
  capture_tampered: null, special_entries: [],
};

function statusOf(checks: TrustCheck[], id: string): TrustStatus {
  const c = checks.find(x => x.id === id);
  if (!c) throw new Error(`no check ${id}`);
  return c.status;
}

function checksFor(patch: Partial<CapsuleVerifyResult>): TrustCheck[] {
  return buildTrustChecks({ ...ANCHORED, ...patch }, '/capsules/run-1');
}

describe('buildTrustChecks — one row per nova verify check', () => {
  it('lists every check in nova verify order', () => {
    expect(buildTrustChecks(ANCHORED).map(c => c.id)).toEqual(CHECK_IDS);
  });

  it('passes every check only when each one ran and passed against supplied anchors', () => {
    const checks = buildTrustChecks(ANCHORED);
    expect(checks.every(c => c.status === 'passed')).toBe(true);
    expect(trustVerdict(ANCHORED, checks)).toMatchObject({ tone: 'passed', headline: 'Every check passed.' });
  });

  it('never claims compliance or correctness from cryptography', () => {
    const v = trustVerdict(ANCHORED, buildTrustChecks(ANCHORED));
    expect(v.detail).toMatch(/not that their content is correct or compliant/);
    expect(v.headline).not.toMatch(/complian|certif|trusted/i);
  });

  it('reads every check as not established for an unsealed capsule', () => {
    const r: CapsuleVerifyResult = { sealed: false, configured: null, message: 'No .seal/ directory' };
    const checks = buildTrustChecks(r);
    expect(checks.map(c => c.status)).toEqual(CHECK_IDS.map(() => 'not_established'));
    expect(trustVerdict(r, checks)).toMatchObject({ tone: 'neutral' });
    expect(trustVerdict(r, checks).headline).toMatch(/^Not sealed/);
  });

  it('never reads a field the server did not send as passed (older server)', () => {
    const r: CapsuleVerifyResult = {
      sealed: true, configured: true, signature_ok: true, timestamp_ok: true,
      log_integrity_ok: true, valid: true,
    };
    const checks = buildTrustChecks(r);
    const passed = checks.filter(c => c.status === 'passed').map(c => c.id);
    expect(passed).toEqual(['signature', 'timestamp-token']);
    expect(trustVerdict(r, checks).tone).toBe('partial');
  });

  it('a sealed result with no fields at all establishes nothing', () => {
    const checks = buildTrustChecks({ sealed: true, configured: true });
    expect(checks.some(c => c.status === 'passed')).toBe(false);
  });

  it('a valid signature from a self-asserted signer is never globally trusted', () => {
    const r = {
      ...ANCHORED, identity_trust: 'self-asserted' as const, ca_chain_checked: false,
      ca_chain_ok: null, unanchored_claims: ['identity-self-asserted'],
    };
    const checks = buildTrustChecks(r);
    expect(statusOf(checks, 'signature')).toBe('passed');
    expect(statusOf(checks, 'signer-identity')).toBe('not_established');
    const v = trustVerdict(r, checks);
    expect(v.tone).toBe('partial');
    expect(v.headline).not.toMatch(/Every check passed/);
    expect(v.detail).toMatch(/identity-self-asserted/);
  });

  it('a local seal CA pin passes but says it is not an external identity', () => {
    const c = checksFor({ identity_trust: 'local-ca-pinned' }).find(x => x.id === 'signer-identity')!;
    expect(c.status).toBe('passed');
    expect(c.reason).toMatch(/not an external identity/);
  });

  it('a failed signer chain is failed, whatever the identity level says', () => {
    expect(statusOf(checksFor({ ca_chain_ok: false, ca_chain_reason: 'unknown issuer' }), 'signer-identity'))
      .toBe('failed');
  });

  it('an absent timestamp is not established and its trust not applicable', () => {
    const r = { ...ANCHORED, timestamp_ok: null, timestamp_present: false, timestamp_trust: 'absent' as const };
    const checks = buildTrustChecks(r);
    expect(statusOf(checks, 'timestamp-token')).toBe('not_established');
    expect(statusOf(checks, 'timestamp-trust')).toBe('not_applicable');
    expect(trustVerdict(r, checks).tone).not.toBe('passed');
  });

  it('an unanchored timestamp: token passed, trusted time not established', () => {
    const checks = checksFor({ timestamp_trust: 'unanchored', tsa_chain_checked: false, tsa_chain_ok: null });
    expect(statusOf(checks, 'timestamp-token')).toBe('passed');
    expect(statusOf(checks, 'timestamp-trust')).toBe('not_established');
  });

  it('a failed or invalid timestamp reads failed', () => {
    expect(statusOf(checksFor({ timestamp_ok: false, timestamp_trust: 'invalid' }), 'timestamp-token')).toBe('failed');
    expect(statusOf(checksFor({ timestamp_trust: 'invalid', tsa_chain_ok: false }), 'timestamp-trust')).toBe('failed');
  });

  it('log inclusion: carried proof passes, the sealer log is then not established', () => {
    const checks = checksFor({ log_inclusion: 'carried-proof', sealer_log: 'no-log', configured: false });
    expect(statusOf(checks, 'log-inclusion')).toBe('passed');
    expect(statusOf(checks, 'sealer-log')).toBe('not_established');
  });

  it.each([
    ['not-checked', 'no-log', 'not_established', 'not_established'],
    ['failed', 'missing', 'failed', 'failed'],
    ['failed', 'not-consulted', 'failed', 'not_applicable'],
    ['carried-proof', 'unverifiable-other-version', 'passed', 'not_established'],
    ['carried-proof', 'not-sealer-log', 'passed', 'not_established'],
  ])('log_inclusion=%s sealer_log=%s', (inclusion, sealer, incStatus, logStatus) => {
    const checks = checksFor({ log_inclusion: inclusion, sealer_log: sealer });
    expect(statusOf(checks, 'log-inclusion')).toBe(incStatus);
    expect(statusOf(checks, 'sealer-log')).toBe(logStatus);
  });

  it('a seal made before evidence_digests leaves the evidence binding not established', () => {
    const c = checksFor({ evidence_digests_present: false, evidence_digests_ok: false })
      .find(x => x.id === 'evidence-binding')!;
    expect(c.status).toBe('not_established');
    expect(c.reason).toMatch(/would not be detected by the seal/);
  });

  it('a modified evidence file fails the evidence binding and names the file', () => {
    const r = {
      ...ANCHORED, valid: false, capsule_binding_ok: false, evidence_digests_ok: false,
      binding_errors: ['evidence file modified after sealing: outputs/stdout.txt'],
    };
    const checks = buildTrustChecks(r);
    const c = checks.find(x => x.id === 'evidence-binding')!;
    expect(c.status).toBe('failed');
    expect(c.evidence).toContain('evidence file modified after sealing: outputs/stdout.txt');
    expect(trustVerdict(r, checks)).toMatchObject({ tone: 'failed' });
  });

  it('a manifest that disagrees, or an unsealed substitute, fails the manifest binding', () => {
    expect(statusOf(checksFor({ manifest_ok: false }), 'manifest-binding')).toBe('failed');
    expect(statusOf(checksFor({ unsealed_manifests: ['capsule.json'] }), 'manifest-binding')).toBe('failed');
    expect(statusOf(checksFor({ manifest_present: false }), 'manifest-binding')).toBe('not_established');
  });

  it('a log entry naming another capsule fails; no capsule_id is not established', () => {
    expect(statusOf(checksFor({ capsule_id_ok: false }), 'capsule-id')).toBe('failed');
    expect(statusOf(checksFor({ capsule_id: '' }), 'capsule-id')).toBe('not_established');
  });

  it('recorded capture tampering and planted links fail', () => {
    expect(statusOf(checksFor({ capture_tampered: 'outputs replaced by a symlink' }), 'capture-integrity'))
      .toBe('failed');
    expect(statusOf(checksFor({ special_entries: ['outputs/x (symlink)'] }), 'special-entries')).toBe('failed');
  });

  it('a failed signature leaves the signed-manifest checks not established', () => {
    const checks = checksFor({ signature_ok: false, valid: false });
    expect(statusOf(checks, 'signature')).toBe('failed');
    expect(statusOf(checks, 'capture-integrity')).toBe('not_established');
    expect(statusOf(checks, 'capsule-id')).toBe('not_established');
  });

  it('an edited signing-intent label fails the signature', () => {
    expect(statusOf(checksFor({ signing_intent_status: 'mismatch' }), 'signature')).toBe('failed');
  });

  it('a verdict of valid=false with no failed row still reads failed', () => {
    const r = { ...ANCHORED, valid: false };
    expect(trustVerdict(r, buildTrustChecks(r))).toMatchObject({ tone: 'failed' });
  });
});

describe('offline nova verify commands', () => {
  it('quotes a capsule path for the shell', () => {
    expect(shellQuote('/capsules/run-1')).toBe('/capsules/run-1');
    expect(shellQuote("/data/my run's")).toBe(`'/data/my run'\\''s'`);
    expect(shellQuote('$(rm -rf ~)')).toBe(`'$(rm -rf ~)'`);
  });

  it('uses nova verify flags that exist and a placeholder without a path', () => {
    const c = verifyCommands('/capsules/run-1');
    expect(c.json).toBe('nova verify --json /capsules/run-1');
    expect(c.caBundle).toContain('--ca-bundle');
    expect(c.tsaBundle).toContain('--tsa-ca-bundle');
    expect(c.sealConfig).toContain('--seal-config');
    expect(c.strict).toMatch(/^nova verify --strict /);
    expect(verifyCommands().json).toBe('nova verify --json <capsule_dir>');
  });

  it('uses only flags the real nova verify has (generated from the Typer app)', () => {
    const verify = GENERATED_COMMANDS.find(c => c.name === 'nova verify');
    expect(verify).toBeTruthy();
    const known = new Set(verify!.fields.filter(f => !f.positional).map(f => f.flag ?? `--${f.key}`));
    const used = Object.values(verifyCommands('/c/R1')).flatMap(c => c.match(/--[a-z-]+/g) ?? []);
    expect(used.length).toBeGreaterThan(0);
    expect(used.filter(f => !known.has(f))).toEqual([]);
  });

  it('points each anchor check at the flag that supplies the anchor', () => {
    const checks = buildTrustChecks(ANCHORED, '/c/R1');
    const cmd = (id: string) => checks.find(c => c.id === id)!.command;
    expect(cmd('signer-identity')).toContain('--ca-bundle');
    expect(cmd('timestamp-trust')).toContain('--tsa-ca-bundle');
    expect(cmd('sealer-log')).toContain('--seal-config');
    expect(cmd('signature')).toBe('nova verify --json /c/R1');
    expect(checks.every(c => c.command.startsWith('nova verify ') && c.command.endsWith('/c/R1'))).toBe(true);
  });

  it('the copyable summary lists every check with its status in words', () => {
    const r = { ...ANCHORED, identity_trust: 'self-asserted' as const };
    const checks = buildTrustChecks(r, '/c/R1');
    const text = trustSummary('R1', r, checks, '/c/R1');
    expect(text).toContain('run R1');
    expect(text).toContain('Signer identity (CA trust anchor): Not established');
    expect(text).toContain('nova verify --json /c/R1');
    expect(text.split('\n').filter(l => l.startsWith('- '))).toHaveLength(CHECK_IDS.length);
  });
});

describe('<TrustChecks>', () => {
  it('renders each check as a labelled article with its status in words', () => {
    render(<TrustChecks result={{ ...ANCHORED, special_entries: ['outputs/x (symlink)'], valid: false }} runId="R1" capsulePath="/c/R1" />);
    const list = screen.getByRole('list', { name: 'Verification checks' });
    const articles = within(list).getAllByRole('article');
    expect(articles).toHaveLength(CHECK_IDS.length);
    const special = screen.getByRole('article', { name: 'No links or special files in the capsule' });
    expect(special).toHaveTextContent('Failed');
    expect(special).toHaveTextContent('outputs/x (symlink)');
    expect(screen.getByTestId('trust-verdict')).toHaveTextContent('Verification failed');
    expect(screen.getAllByRole('button', { name: /^Copy nova verify --json \/c\/R1$/ }).length).toBeGreaterThan(0);
  });

  it('says what the server checked with, and how to check independently', () => {
    render(<TrustChecks result={{ ...ANCHORED, configured: false }} runId="R1" />);
    expect(screen.getByTestId('trust-checks')).toHaveTextContent('from its own files');
    expect(screen.getByTestId('trust-checks')).toHaveTextContent('needs no server and no network');
    expect(screen.getByTestId('trust-check-signature')).toHaveTextContent('nova verify --json <capsule_dir>');
  });
});

describe('IntegrityPanel + per-check list', () => {
  it('shows every check after a verification and qualifies a partial pass', async () => {
    const user = userEvent.setup();
    const verify = async () => ({
      ...ANCHORED, configured: false, identity_trust: 'self-asserted' as const,
      ca_chain_checked: false, ca_chain_ok: null, log_inclusion: 'carried-proof', sealer_log: 'no-log',
      unanchored_claims: ['identity-self-asserted', 'log-head-unanchored'],
    });
    render(<IntegrityPanel runId="R1" verify={verify} capsulePath="/c/R1" />);
    expect(screen.queryByTestId('trust-checks')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Verify seal' }));
    expect(await screen.findByTestId('integrity-verdict')).toHaveTextContent('not every check is established');
    expect(screen.getByTestId('trust-check-signer-identity')).toHaveAttribute('data-status', 'not_established');
    expect(screen.getByTestId('trust-check-signer-identity')).toHaveTextContent('--ca-bundle <ca-bundle.pem> /c/R1');
    expect(screen.getByTestId('trust-verdict')).toHaveTextContent('Partially established');
  });

  it('an unsealed capsule shows every check as not established', async () => {
    const user = userEvent.setup();
    render(<IntegrityPanel runId="R1" verify={async () => ({ sealed: false, configured: null })} />);
    await user.click(screen.getByRole('button', { name: 'Verify seal' }));
    await screen.findByTestId('trust-checks');
    for (const id of CHECK_IDS) {
      expect(screen.getByTestId(`trust-check-${id}`)).toHaveAttribute('data-status', 'not_established');
    }
  });
});
