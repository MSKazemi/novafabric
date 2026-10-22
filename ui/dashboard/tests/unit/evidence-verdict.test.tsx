/**
 * Evidence tab verdict badges (M11).
 *
 * The Integrity column used to render only `sig`, so a bundle whose content was
 * tampered with under an intact signature showed `sig ✓`. These tests pin the
 * honesty contract: content checks are shown, an unanchored signer is never a
 * check mark, and the overall verdict is visible.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import EvidenceList from '@/components/evidence/EvidenceList';
import { api, type EvidenceSummary, type VerifyResult } from '@/lib/api';
import { contentBadge, signerBadge } from '@/lib/evidenceVerdict';

const BASE: VerifyResult = {
  bundle_id: 'b1',
  run_id: 'r1',
  valid: true,
  signature_ok: true,
  timestamp_ok: null,
  log_integrity_ok: null,
  seal_available: false,
  errors: [],
  manifest_hash_ok: true,
  artifacts_ok: true,
  complete: true,
  no_extras: true,
  subjects_ok: true,
  attestations_verified: 3,
  signer_trust: 'unanchored',
  signer_fingerprints: ['sha256:abc'],
};

describe('contentBadge', () => {
  it('passes only when every recomputed check passes', () => {
    expect(contentBadge(BASE).ok).toBe(true);
    expect(contentBadge({ ...BASE, subjects_ok: false }).ok).toBe(false);
    expect(contentBadge({ ...BASE, artifacts_ok: false }).ok).toBe(false);
  });

  it('is neutral for an older server that reports no content checks', () => {
    const {
      manifest_hash_ok: _m,
      artifacts_ok: _a,
      complete: _c,
      no_extras: _n,
      subjects_ok: _s,
      ...old
    } = BASE;
    expect(contentBadge(old as VerifyResult).ok).toBeNull();
  });
});

describe('signerBadge', () => {
  it('never draws an unanchored signer as a check mark', () => {
    const badge = signerBadge(BASE)!;
    expect(badge.ok).toBeNull();
    expect(badge.label).toContain('unanchored');
    expect(badge.title).toContain('sha256:abc');
  });

  it('marks anchored as trusted and untrusted as failed', () => {
    expect(signerBadge({ ...BASE, signer_trust: 'anchored' })!.ok).toBe(true);
    expect(signerBadge({ ...BASE, signer_trust: 'untrusted' })!.ok).toBe(false);
    expect(signerBadge({ ...BASE, signer_trust: 'none' })!.ok).toBe(false);
  });

  it('renders nothing for a server that does not report signer trust', () => {
    expect(signerBadge({ ...BASE, signer_trust: undefined })).toBeNull();
  });
});

describe('EvidenceList verify result', () => {
  afterEach(() => vi.restoreAllMocks());

  const bundles: EvidenceSummary[] = [
    { bundle_id: 'b1', run_id: 'r1', timestamp: '', size_bytes: 10, verified: true },
  ];

  it('shows invalid and a failed content badge for a tampered bundle with an intact signature', async () => {
    vi.spyOn(api, 'verifyEvidence').mockResolvedValue({
      ...BASE,
      valid: false,
      subjects_ok: false,
      errors: ['subject: attestations/run.intoto.json (the capsule copy does not hash to the signed subject)'],
    });
    render(
      <EvidenceList bundles={bundles} selectedId={null} onSelect={() => {}} onRefresh={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() => expect(screen.getByText(/^invalid/)).toBeTruthy());
    expect(screen.getByText(/^content ✗/)).toBeTruthy();
    expect(screen.getByText(/^signer unanchored/)).toBeTruthy();
    expect(screen.getByText(/^sig ✓/)).toBeTruthy();
  });
});
