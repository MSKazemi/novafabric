import type { VerifyResult } from './api';

/** One inline badge in the Evidence tab's Integrity column. `ok: null` renders neutral. */
export interface VerdictBadge {
  label: string;
  ok: boolean | null;
  title: string;
}

/**
 * Content badge: manifest_hash, every artifact digest, no missing/extra member, and
 * every signed subject recomputed from the bundle (M11, the `nova verify` recipe).
 * `null` for an older server that does not report these checks.
 */
export function contentBadge(r: VerifyResult): VerdictBadge {
  const parts = [r.manifest_hash_ok, r.artifacts_ok, r.complete, r.no_extras, r.subjects_ok];
  const ok = parts.every((p) => p === undefined) ? null : parts.every((p) => p === true);
  return {
    label: 'content',
    ok,
    title:
      'Artifact digests, manifest_hash and signed subjects recomputed from the bundle — the same checks as `nova verify <bundle>.zip`.',
  };
}

/**
 * Signer badge. The verifying key ships inside the bundle, so a passing signature only
 * proves consistency; `anchored` means every signer is in the operator's trust list
 * (NOVAFABRIC_EVIDENCE_TRUSTED_KEYS). `unanchored` is neutral, never a check mark.
 */
export function signerBadge(r: VerifyResult): VerdictBadge | null {
  const fps = (r.signer_fingerprints ?? []).join(', ');
  switch (r.signer_trust) {
    case 'anchored':
      return { label: 'signer', ok: true, title: `Signed by a trusted key: ${fps}` };
    case 'untrusted':
      return { label: 'signer untrusted', ok: false, title: `Signer is not in the trust list: ${fps}` };
    case 'unanchored':
      return {
        label: 'signer unanchored',
        ok: null,
        title:
          `Origin not established: signatures verify only under the key shipped in the bundle (${fps}). ` +
          'Configure NOVAFABRIC_EVIDENCE_TRUSTED_KEYS on the server to pin signers.',
      };
    case 'none':
      return { label: 'signer', ok: false, title: 'No attestation signature verified.' };
    default:
      return null;
  }
}
