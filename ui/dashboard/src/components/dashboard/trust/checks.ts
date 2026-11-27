/**
 * Trust Center — per-check model of one capsule verification (issue #271).
 *
 * Pure: turns the `POST /api/runs/{id}/verify` result into one row per check
 * `nova verify <capsule-dir>` performs, in the order its text report prints them.
 * The rules, which the unit tests pin:
 *
 * - A check reads **passed** only when the server reported that it ran and
 *   passed. A field the server did not send (older server, config error, an
 *   unsealed capsule) reads **not established**, never passed.
 * - **Not established** is also what a check that ran but rests on nothing the
 *   verifier trusts reads: a self-asserted signer, an unanchored timestamp, no
 *   sealer log, a seal made before `evidence_digests`.
 * - A check that compares files with the signed payload (manifest and evidence
 *   binding, capsule_id, capture record) never passes unless the signature
 *   verified: the payload is decoded without checking the signature.
 * - **Not applicable** is used only where the check has nothing to judge (no
 *   timestamp token, so no time to anchor).
 * - Nothing here claims factual correctness, compliance or certification: a
 *   check states what the cryptography established about these files.
 */
import type { CapsuleVerifyResult } from '../../../lib/api';

export type TrustStatus = 'passed' | 'failed' | 'not_established' | 'not_applicable';

export const STATUS_WORDS: Record<TrustStatus, string> = {
  passed: 'Passed',
  failed: 'Failed',
  not_established: 'Not established',
  not_applicable: 'Not applicable',
};

export interface TrustCheck {
  id: string;
  name: string;
  status: TrustStatus;
  /** Why the check has this status, in words. */
  reason: string;
  /** What the check looked at: capsule files, fields, anchors. */
  evidence: string[];
  /** The exact `nova verify …` command an auditor runs offline for this check. */
  command: string;
}

/** Placeholder used when the capsule path is not known. */
export const CAPSULE_PLACEHOLDER = '<capsule_dir>';

const NOT_REPORTED = 'The server did not report this check, so it is not established here.';
const NOT_SEALED = 'The capsule has no .seal/ directory, so there is nothing to check.';
/** The binding compares files with the envelope's payload, which only a verified signature makes "the signed" one. */
const UNSIGNED_PAYLOAD =
  'The files match the payload in the envelope, but the signature did not verify, so that payload is not shown to be the one that was signed.';

/** POSIX shell quoting for a path an auditor will paste into a terminal. */
export function shellQuote(value: string): string {
  if (value === CAPSULE_PLACEHOLDER) return value;
  if (/^[\w@%+=:,./-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** The `nova verify` commands each check points to (flags as in `nova verify --help`). */
export function verifyCommands(capsulePath?: string | null) {
  const dir = shellQuote(capsulePath && capsulePath.trim() ? capsulePath : CAPSULE_PLACEHOLDER);
  return {
    json: `nova verify --json ${dir}`,
    caBundle: `nova verify --json --ca-bundle <ca-bundle.pem> ${dir}`,
    tsaBundle: `nova verify --json --tsa-ca-bundle <tsa-ca.pem> ${dir}`,
    sealConfig: `nova verify --json --seal-config <sealer-novaseal.yaml> ${dir}`,
    strict: `nova verify --strict --ca-bundle <ca-bundle.pem> --tsa-ca-bundle <tsa-ca.pem> ${dir}`,
  };
}

type Check = Omit<TrustCheck, 'command'>;

function signature(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'signature',
    name: 'Signature (DSSE ECDSA P-256)',
    evidence: [
      '.seal/manifest.dsse (the DSSE envelope)',
      ...(r.signer_subject ? [`embedded signer certificate: ${r.signer_subject} (a claim of the envelope)`] : []),
    ],
  };
  if (r.signature_ok === undefined) return { ...base, status: 'not_established', reason: NOT_REPORTED };
  if (r.signing_intent_status === 'mismatch') {
    return { ...base, status: 'failed', reason: 'The signing-intent label was edited after signing.' };
  }
  if (!r.signature_ok) {
    return {
      ...base,
      status: 'failed',
      reason: 'The signature does not verify over the signed manifest with the key the envelope names.',
    };
  }
  const notes: string[] = [];
  if (r.pae_encoding === 'legacy-le64') {
    notes.push('Signed over the pre-spec PAE (NovaFabric ≤ v0.102.x): stock DSSE tools will not verify it.');
  }
  if (r.signing_intent_status === 'label-only') {
    notes.push('The signing-intent label predates signed intents and is not authenticated.');
  }
  return {
    ...base,
    status: 'passed',
    reason: ['The signature verifies under the key in the envelope’s own certificate.', ...notes].join(' '),
  };
}

function timestampToken(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'timestamp-token',
    name: 'Timestamp token (RFC 3161)',
    evidence: [
      '.seal/manifest.dsse.tsr',
      ...(r.timestamp_gen_time ? [`claimed time: ${r.timestamp_gen_time}`] : []),
    ],
  };
  if (r.timestamp_ok === undefined) return { ...base, status: 'not_established', reason: NOT_REPORTED };
  if (r.timestamp_ok === null || r.timestamp_trust === 'absent') {
    return {
      ...base,
      status: 'not_established',
      reason: 'No RFC 3161 token in the capsule: it claims no time. Timestamping is opt-in, so nova verify does not fail on this.',
    };
  }
  if (!r.timestamp_ok) {
    return { ...base, status: 'failed', reason: 'A token is present but is not intact or not bound to this envelope.' };
  }
  return {
    ...base,
    status: 'passed',
    reason: r.timestamp_strict === false
      ? 'The token is intact and bound to this envelope (only the legacy structural check could run).'
      : 'The token is intact and bound to this envelope.',
  };
}

function timestampTrust(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'timestamp-trust',
    name: 'Trusted time (TSA trust anchor)',
    evidence: [
      ...(r.tsa_subject ? [`TSA certificate: ${r.tsa_subject}`] : []),
      r.tsa_chain_checked
        ? 'TSA trust anchors configured on the verifying host (tsa_ca_certs)'
        : 'no TSA trust anchor was supplied to the verifier',
    ],
  };
  if (r.tsa_chain_checked && r.tsa_chain_ok === false) {
    return {
      ...base,
      status: 'failed',
      reason: `The TSA certificate chain did not reach a trust anchor${r.tsa_chain_reason ? `: ${r.tsa_chain_reason}` : '.'}`,
    };
  }
  switch (r.timestamp_trust) {
    case 'anchored':
      return { ...base, status: 'passed', reason: 'The TSA chain reached a TSA trust anchor the verifier supplied: the time is trusted.' };
    case 'unanchored':
      return {
        ...base,
        status: 'not_established',
        reason: 'The token is intact, but no TSA trust anchor vouched for its issuer: the time is a claim, not trusted time.',
      };
    case 'absent':
      return { ...base, status: 'not_applicable', reason: 'No timestamp token, so there is no time to anchor.' };
    case 'invalid':
      return { ...base, status: 'failed', reason: 'The token failed its integrity check or its anchored chain check.' };
    default:
      return { ...base, status: 'not_established', reason: NOT_REPORTED };
  }
}

function logInclusion(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'log-inclusion',
    name: 'Merkle log inclusion',
    evidence: ['.seal/log-entry.json (the inclusion proof the capsule carries)', ...(r.log_notes ?? [])],
  };
  switch (r.log_inclusion) {
    case 'local-log':
      return { ...base, status: 'passed', reason: 'The entry is in the sealer’s Merkle log available to the verifier.' };
    case 'carried-proof':
      return {
        ...base,
        status: 'passed',
        reason: 'The inclusion proof the capsule carries verifies. Its tree head is not signed or witnessed, so the log itself is not established (see Sealer’s Merkle log).',
      };
    case 'not-checked':
      return {
        ...base,
        status: 'not_established',
        reason: 'The capsule carries no inclusion proof and no available log holds its entry (sealed by NovaFabric ≤ v0.102.x).',
      };
    case 'failed':
      return { ...base, status: 'failed', reason: 'The log metadata is inconsistent with the capsule or with the log.' };
    default:
      return { ...base, status: 'not_established', reason: NOT_REPORTED };
  }
}

function sealerLog(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'sealer-log',
    name: 'Sealer’s Merkle log',
    evidence: [r.configured ? 'the Merkle log(s) named in this host’s novaseal.yaml' : 'no novaseal.yaml on the verifying host, so no local log'],
  };
  switch (r.sealer_log) {
    case 'entry-found':
      return r.log_inclusion === 'failed'
        ? { ...base, status: 'failed', reason: 'The sealer’s log holds an entry for this capsule, but it does not match.' }
        : { ...base, status: 'passed', reason: 'The sealer’s log holds this capsule’s entry.' };
    case 'no-log':
      return { ...base, status: 'not_established', reason: 'No Merkle log was available to the verifier; inclusion rests on the capsule’s own proof.' };
    case 'not-sealer-log':
      return { ...base, status: 'not_established', reason: 'The available log holds no seals by this signer (another sealer’s log, or a new one).' };
    case 'unverifiable-other-version':
      return {
        ...base,
        status: 'not_established',
        reason: 'The signer’s log is available only under a newer root version; list the retired log in previous_merkle_dbs.',
      };
    case 'missing':
      return { ...base, status: 'failed', reason: 'The sealer’s log does not hold this capsule’s entry.' };
    case 'not-consulted':
      return { ...base, status: 'not_applicable', reason: 'Inclusion failed on the capsule’s own log metadata before any log was consulted.' };
    default:
      return { ...base, status: 'not_established', reason: NOT_REPORTED };
  }
}

function manifestBinding(r: CapsuleVerifyResult): Check {
  const file = r.manifest_file ?? 'capsule.yaml';
  const base = {
    id: 'manifest-binding',
    name: `Manifest binding (${file} matches the signed payload)`,
    evidence: [file, '.seal/manifest.dsse (signed payload)', ...(r.unsealed_manifests ?? []).map(m => `unsealed manifest: ${m}`), ...(r.unsealed_refs ?? []).map(m => `unsealed reference: ${m}`)],
  };
  if (r.manifest_present === undefined) return { ...base, status: 'not_established', reason: NOT_REPORTED };
  const unsealed = (r.unsealed_manifests?.length ?? 0) + (r.unsealed_refs?.length ?? 0);
  if (unsealed > 0) {
    return { ...base, status: 'failed', reason: 'A manifest or a signed reference that readers trust is not covered by the seal.' };
  }
  if (!r.manifest_present) {
    return { ...base, status: 'not_established', reason: 'No manifest file to compare with the signed payload.' };
  }
  if (!r.manifest_ok) {
    return { ...base, status: 'failed', reason: `${file} on disk disagrees with the signed payload.` };
  }
  if (!r.signature_ok) return { ...base, status: 'not_established', reason: UNSIGNED_PAYLOAD };
  return { ...base, status: 'passed', reason: `${file} on disk matches the signed payload.` };
}

function evidenceBinding(r: CapsuleVerifyResult): Check {
  const fileErrors = (r.binding_errors ?? []).filter(e => e.startsWith('evidence file'));
  const base = {
    id: 'evidence-binding',
    name: 'Evidence binding (per-file sha256)',
    evidence: ['evidence_digests in the signed payload', 'each evidence file in the capsule', ...fileErrors],
  };
  if (r.evidence_digests_present === undefined) return { ...base, status: 'not_established', reason: NOT_REPORTED };
  if (!r.evidence_digests_present) {
    return {
      ...base,
      status: 'not_established',
      reason: 'Sealed before evidence_digests: the seal signs the manifest only, so an edit to the evidence files would not be detected by the seal.',
    };
  }
  if (!r.evidence_digests_ok) {
    return { ...base, status: 'failed', reason: 'An evidence file was modified or removed after sealing.' };
  }
  if (!r.signature_ok) return { ...base, status: 'not_established', reason: UNSIGNED_PAYLOAD };
  return { ...base, status: 'passed', reason: 'Every evidence file the seal lists has the sha256 it signed.' };
}

function capsuleId(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'capsule-id',
    name: 'Log-entry capsule_id matches the signed payload',
    evidence: ['.seal/log-entry.json capsule_id', ...(r.capsule_id ? [`capsule_id: ${r.capsule_id}`] : []), 'sha256 of the signed payload'],
  };
  if (r.capsule_id_ok === undefined) return { ...base, status: 'not_established', reason: NOT_REPORTED };
  if (!r.capsule_id_ok) {
    return { ...base, status: 'failed', reason: 'log-entry.json names a different capsule than the one that was signed.' };
  }
  if (!r.capsule_id || !r.signature_ok) {
    return { ...base, status: 'not_established', reason: 'There was no capsule_id or no verified signed payload to compare.' };
  }
  return { ...base, status: 'passed', reason: 'log-entry.json names the capsule that was signed.' };
}

function signerIdentity(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'signer-identity',
    name: 'Signer identity (CA trust anchor)',
    evidence: [
      ...(r.signer_subject ? [`embedded signer certificate: ${r.signer_subject}`] : []),
      r.ca_chain_checked
        ? 'CA bundle configured on the verifying host (ca_bundle)'
        : 'no CA bundle was supplied to the verifier',
    ],
  };
  const statement = r.identity_statement ? ` (${r.identity_statement})` : '';
  if (r.ca_chain_checked && r.ca_chain_ok === false) {
    return {
      ...base,
      status: 'failed',
      reason: `The signer certificate chain did not reach an anchor in the CA bundle${r.ca_chain_reason ? `: ${r.ca_chain_reason}` : '.'}`,
    };
  }
  switch (r.identity_trust) {
    case 'ca-anchored':
      return { ...base, status: 'passed', reason: `The signer certificate chains to a CA anchor the verifier supplied${statement}.` };
    case 'local-ca-pinned':
      return {
        ...base,
        status: 'passed',
        reason: `The signer chains to the NovaFabric local seal CA the verifier supplied: continuity with that installation, not an external identity${statement}.`,
      };
    case 'self-asserted':
      return { ...base, status: 'not_established', reason: `Self-asserted: nothing the verifier trusts vouches for the signing key${statement}.` };
    case 'none':
      return { ...base, status: 'not_established', reason: 'No signature verified, so nothing about the signer is attested.' };
    default:
      return { ...base, status: 'not_established', reason: NOT_REPORTED };
  }
}

function captureIntegrity(r: CapsuleVerifyResult): Check {
  const base = {
    id: 'capture-integrity',
    name: 'Capture integrity (no workload tampering recorded)',
    evidence: ['metadata.capture_tampered in the signed manifest'],
  };
  if (r.capture_tampered === undefined) return { ...base, status: 'not_established', reason: NOT_REPORTED };
  if (r.capture_tampered !== null) {
    return { ...base, status: 'failed', reason: `The capture recorded tampering by the workload: ${r.capture_tampered}` };
  }
  if (!r.signature_ok) {
    return { ...base, status: 'not_established', reason: 'The signed manifest did not verify, so its record cannot be relied on.' };
  }
  return { ...base, status: 'passed', reason: 'The signed manifest records no tampering by the workload during capture.' };
}

function specialEntries(r: CapsuleVerifyResult): Check {
  const entries = r.special_entries ?? [];
  const base = {
    id: 'special-entries',
    name: 'No links or special files in the capsule',
    evidence: ['every entry in the capsule directory', ...entries.slice(0, 10), ...(entries.length > 10 ? [`… and ${entries.length - 10} more`] : [])],
  };
  if (r.special_entries === undefined) return { ...base, status: 'not_established', reason: NOT_REPORTED };
  if (entries.length > 0) {
    return { ...base, status: 'failed', reason: 'A symlink, FIFO, socket or device was planted in the capsule. NovaFabric never writes one.' };
  }
  return { ...base, status: 'passed', reason: 'No symlinks, FIFOs, sockets or devices in the capsule directory.' };
}

const COMMAND_FOR: Record<string, keyof ReturnType<typeof verifyCommands>> = {
  'timestamp-trust': 'tsaBundle',
  'sealer-log': 'sealConfig',
  'signer-identity': 'caBundle',
};

/** Every check `nova verify <capsule-dir>` performs, with its own status. */
export function buildTrustChecks(r: CapsuleVerifyResult, capsulePath?: string | null): TrustCheck[] {
  const commands = verifyCommands(capsulePath);
  const rows = [
    signature, timestampToken, timestampTrust, logInclusion, sealerLog,
    manifestBinding, evidenceBinding, capsuleId, signerIdentity, captureIntegrity, specialEntries,
  ].map(fn => fn(r));
  return rows.map(row => {
    const sealed: Check = r.sealed === false ? { ...row, status: 'not_established', reason: NOT_SEALED } : row;
    return { ...sealed, command: commands[COMMAND_FOR[row.id] ?? 'json'] };
  });
}

export interface TrustVerdict {
  tone: 'passed' | 'failed' | 'partial' | 'neutral';
  headline: string;
  detail: string;
}

/** One sentence over all checks — never stronger than the weakest check allows. */
export function trustVerdict(r: CapsuleVerifyResult, checks: TrustCheck[]): TrustVerdict {
  const count = (s: TrustStatus) => checks.filter(c => c.status === s).length;
  const failed = count('failed');
  const open = count('not_established');
  if (r.sealed === false) {
    return { tone: 'neutral', headline: 'Not sealed: nothing about this capsule is verified.', detail: r.message ?? NOT_SEALED };
  }
  if (r.valid === false || failed > 0) {
    return {
      tone: 'failed',
      headline: failed > 0
        ? `Verification failed: ${failed} of ${checks.length} checks failed.`
        : 'Verification failed: nova verify rejects this capsule.',
      detail: 'nova verify exits 1 on this capsule. The errors the verifier reported are listed above.',
    };
  }
  if (r.valid === undefined) {
    return { tone: 'neutral', headline: 'No verdict: the server did not return one.', detail: NOT_REPORTED };
  }
  if (open > 0) {
    return {
      tone: 'partial',
      headline: `Partially established: ${open} of ${checks.length} checks are not established.`,
      detail: (r.unanchored_claims?.length ?? 0) > 0
        ? `nova verify exits 0, but these checks rest on nothing the verifier trusts or did not run. nova verify --strict fails on: ${r.unanchored_claims!.join(', ')}.`
        : 'nova verify exits 0, but these checks rest on nothing the verifier trusts or did not run.',
    };
  }
  return {
    tone: 'passed',
    headline: 'Every check passed.',
    detail: 'A passed check states what the cryptography established about these files, not that their content is correct or compliant.',
  };
}

/** A plain-text summary a reviewer can paste into a ticket or report. */
export function trustSummary(
  runId: string,
  r: CapsuleVerifyResult,
  checks: TrustCheck[],
  capsulePath?: string | null,
): string {
  const verdict = trustVerdict(r, checks);
  const commands = verifyCommands(capsulePath);
  const lines = [
    `NovaFabric verification of run ${runId}`,
    verdict.headline,
    '',
    ...checks.map(c => `- ${c.name}: ${STATUS_WORDS[c.status]} — ${c.reason}`),
    '',
    `Check it yourself, offline: ${commands.json}`,
    `Require every claim to be anchored: ${commands.strict}`,
  ];
  return lines.join('\n');
}
