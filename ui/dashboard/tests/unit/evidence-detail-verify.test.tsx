/**
 * Evidence detail "Verify bundle" (review r8).
 *
 * The panel used to verify one DSSE signature in the browser, against a certificate
 * path real bundles do not have, ignoring every member digest. It now shows the
 * verdict of the server's verifier -- the same recipe as `nova verify <bundle>.zip` --
 * and only a fully signed bundle with an operator-anchored signer is a check mark.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import EvidenceDetailPanel from '@/components/evidence/EvidenceDetail';
import { api, type EvidenceDetail, type VerifyResult } from '@/lib/api';

const DETAIL: EvidenceDetail = {
  bundle_id: 'b1',
  run_id: 'r1',
  timestamp: '2026-10-23T00:00:00Z',
  size_bytes: 1024,
  manifest: {},
  dsse_statement: {},
  dsse_envelope: {},
  signing_key_fingerprint: 'sha256:abc',
  files: [],
};

const OK: VerifyResult = {
  bundle_id: 'b1',
  run_id: 'r1',
  valid: true,
  signature_ok: true,
  timestamp_ok: null,
  log_integrity_ok: null,
  seal_available: false,
  errors: [],
  signer_trust: 'anchored',
  signer_fingerprints: ['sha256:abc'],
  members_signed: true,
  unsigned_members: [],
};

async function verifyWith(result: VerifyResult): Promise<void> {
  vi.spyOn(api, 'getEvidenceDetail').mockResolvedValue(DETAIL);
  vi.spyOn(api, 'verifyEvidence').mockResolvedValue(result);
  render(<EvidenceDetailPanel bundleId="b1" onBack={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: /verify bundle/i }));
}

afterEach(() => vi.restoreAllMocks());

describe('EvidenceDetail verify', () => {
  it('asks the server verifier, never verifies in the browser', async () => {
    await verifyWith(OK);
    await waitFor(() => expect(api.verifyEvidence).toHaveBeenCalledWith('b1'));
  });

  it('is a check mark only for a fully signed bundle with an anchored signer', async () => {
    await verifyWith(OK);
    expect(await screen.findByText(/operator-trusted key/)).toBeTruthy();
    expect(screen.queryByText(/^⚠/)).toBeNull();
  });

  it('warns, not passes, when the signer is not pinned', async () => {
    await verifyWith({ ...OK, signer_trust: 'unanchored' });
    expect(await screen.findByText(/not pinned/)).toBeTruthy();
    expect(screen.queryByText(/operator-trusted key/)).toBeNull();
  });

  it('warns and lists unsigned members of a legacy bundle', async () => {
    await verifyWith({ ...OK, members_signed: false, unsigned_members: ['README.md'] });
    expect(await screen.findByText(/not covered by any signature/)).toBeTruthy();
    expect(screen.getByText('README.md')).toBeTruthy();
  });

  it('fails with the server errors for an invalid bundle', async () => {
    await verifyWith({ ...OK, valid: false, errors: ['README.md: digest mismatch'] });
    expect(await screen.findByText(/Verification FAILED/)).toBeTruthy();
    expect(screen.getByText('README.md: digest mismatch')).toBeTruthy();
  });
});
