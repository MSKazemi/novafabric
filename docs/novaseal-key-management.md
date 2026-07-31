# NovaSeal Key Management Guide

**Status:** Works today (ADR-0041, NovaSeal v0.1+)

This document covers the full key lifecycle for NovaSeal: generation,
deployment, rotation, compromise recovery, and multi-region availability.

---

## 1. Key Types and Algorithms

NovaSeal's default `local` signing profile signs capsules with **ECDSA P-256 /
SHA-256** (DSSE); the cloud-KMS profiles (`aws_kms`, `azure_kv`, `gcp_kms`) use
the same P-256 curve — this is hardcoded, not a choice per deployment:
`AwsKmsSigningBackend` always requests `SigningAlgorithm="ECDSA_SHA_256"`,
`AzureKvSigningBackend` always requests `SignatureAlgorithm.es256`, and
`GcpKmsSigningBackend` expects an EC P-256 asymmetric key — **a key created
with any other curve or algorithm (e.g. Ed25519) will fail to sign through
these backends.** Ed25519 (RFC 8032) is used only on the alternate
evidence-bundle / collector signer path (`evidence/signing.py`), not for the
default capsule seal. ECDSA P-256 was chosen for the seal core because:
- It provides 128-bit security and is a NIST/FIPS-approved signature scheme.
- Key generation and signing are well-supported across HSMs and cloud KMS.
- It is supported natively by all major HSM vendors and cloud KMS providers
  (AWS KMS, Azure Key Vault, and GCP Cloud KMS all sign with ECDSA over P-256).

Each of these four signing profiles has:
- One **private signing key** (never leaves the HSM or encrypted key store).
- One **public verification key** (distributed to verifiers).
- An optional **certificate** (PEM, for X.509-anchored verification chains).

Two further signing paths exist outside the `local`/`aws_kms`/`azure_kv`/
`gcp_kms` profile system in `novaseal.yaml`, selected instead via a CLI flag
on `nova seal sign` — see §2.3 and §2.4:

- **`x509` cert-pinned identity** (ADR-0055, **works today** as a library API;
  ECDSA P-256 or RSA-2048+, `trust/novaseal/x509_identity.py`) — not yet
  wired to `nova seal`/`novaseal.yaml`.
- **Sigstore keyless** (ADR-0071, **works today** via `nova seal sign
  --backend sigstore`) — no long-lived private key at all.

---

## 2. Key Generation

### 2.1 HSM-backed (Production Minimum)

**Corrected 2026-07-30:** the examples below generate an **ECDSA P-256** key,
not Ed25519 — NovaSeal's `local`/`aws_kms`/`azure_kv`/`gcp_kms` profiles all
sign with P-256 (§1); a key generated as Ed25519 will be rejected or fail at
sign time by these backends. This was previously a real inconsistency in this
doc. If your HSM/KMS key is meant for the alternate Ed25519 evidence-bundle
signer path instead, it is out of scope for this section.

**YubiHSM 2 (recommended for on-premises):**

```bash
# Generate key in slot 1, ECDSA P-256, auth key 1
yubihsm-shell --authkey 1 --password <password> \
  --action generate-asymmetric-key \
  --key-id 1 \
  --label "novaseal-production" \
  --capabilities sign-ecdsa \
  --algorithm ecp256
```

Export the public key:
```bash
yubihsm-shell --authkey 1 --password <password> \
  --action get-public-key --key-id 1 \
  --out novaseal-public.pem
```

**GCP Cloud KMS (recommended for GCP deployments):**

```bash
gcloud kms keys create novaseal-production \
  --location us-central1 \
  --keyring novafabric \
  --purpose asymmetric-signing \
  --default-algorithm ec-sign-p256-sha256
```

**AWS KMS (recommended for AWS deployments — this is what the `aws_kms`
profile actually talks to; AWS CloudHSM is a separate product and is not
the backend `AwsKmsSigningBackend` calls):**

```bash
aws kms create-key \
  --key-spec ECC_NIST_P256 \
  --key-usage SIGN_VERIFY
```

### 2.2 Software Key Store (Development / Air-gapped Minimum)

For development or air-gapped environments where an HSM is not available:

```bash
# Generate an ECDSA P-256 private key (PKCS#8 PEM) and a self-signed certificate
# (this is the same procedure as the local profile in the Configuration Reference §2.1)
openssl ecparam -name prime256v1 -genkey -noout | \
  openssl pkcs8 -topk8 -nocrypt -out ~/.novafabric/keys/seal.key

openssl req -new -x509 -key ~/.novafabric/keys/seal.key \
  -out ~/.novafabric/keys/seal.crt \
  -days 3650 -subj "/CN=NovaSeal-Local"

# This produces:
#   ~/.novafabric/keys/seal.key   (ECDSA P-256 private key — PROTECT THIS FILE)
#   ~/.novafabric/keys/seal.crt   (X.509 certificate — reference from cert_path)
```

**Security requirements for software key storage:**
- Private key file must be `chmod 600` (owner read-only).
- The directory must be `chmod 700`.
- The private key must NOT be committed to any version control system.
- At rest encryption (LUKS / BitLocker / FileVault) is required for the partition.
- Consider moving to an HSM at the earliest opportunity.

### 2.3 `x509` cert-pinned identity (ADR-0055) — works today, library API only

`trust/novaseal/x509_identity.py` (shipped v0.91.0) is a distinct signing
identity from the four profiles above: it signs with a long-lived ECDSA
P-256 **or** RSA-2048+ key, embeds the operator-issued X.509 certificate
alongside every signature, and verifies **offline with no CA path-building**
— trust is a pinned set of certificate SHA-256 fingerprints the operator
maintains, not a certificate chain to a root. This is deliberately
air-gap-friendly.

```python
from novafabric.trust.novaseal.x509_identity import (
    X509SigningIdentity, verify_x509_signature,
)

identity = X509SigningIdentity.from_pem(key_pem, cert_pem)
sig = identity.sign(payload)                     # X509Signature
result = verify_x509_signature(
    payload, sig, pinned_fingerprints={identity.certificate_fingerprint},
)
# result.valid, result.reason, result.subject_common_name
```

Key generation is the same `openssl ecparam`/`openssl req -x509` procedure as
§2.2 (or an RSA-2048+ equivalent). Rotation and compromise recovery follow
the same procedures as §3/§4 — the trust anchor to update is your
`pinned_fingerprints` set, not a CA.

**Honest limits:** this is a working library primitive with passing tests
(`tests/seal/test_x509_identity.py`), not yet wired into `novaseal.yaml` or
any `nova seal`/`nova verify` CLI flag. Full CA-bundle chain validation and
the Rekor inclusion-proof option (ADR-0055's remaining verification step) are
**future design**.

### 2.4 Sigstore keyless (ADR-0071) — works today via `nova seal sign --backend sigstore`

No persistent private key to generate, distribute, or rotate: `nova seal
sign <manifest> --backend sigstore` obtains a short-lived OIDC identity token,
exchanges it for an ephemeral Fulcio certificate, signs with the ephemeral
key, and submits the certificate + signature to Rekor for an inclusion proof.
Requires `pip install novafabric[sigstore]`. Verify with `nova verify
--backend sigstore --capsule-id <id>`.

Because there is no long-lived key, none of the rotation/compromise/
multi-region guidance in §3–§5 applies to this path — the "key" is a fresh
Fulcio certificate per signature, scoped to the signer's OIDC identity
instead.

---

## 3. Rotation Procedure

Rotate keys on any of these triggers:
- Scheduled rotation (recommended: every 12 months for the ECDSA P-256 keys
  used by the `local`/cloud-KMS profiles and the `x509` identity; the same
  cadence is reasonable for the alternate Ed25519 evidence-bundle key).
- Security incident or suspected compromise (see §4).
- Personnel change (key holder departs the organisation).
- Algorithm deprecation notice.

**Rotation steps:**

1. Generate a new key pair using the procedure in §2.
2. Update the NovaSeal signing profile to reference the new key (`profile`
   must be one of `local`/`aws_kms`/`azure_kv`/`gcp_kms` — see
   [Configuration Reference §2](novaseal-configuration.md#2-profiles)):
   ```yaml
   # ~/.novafabric/novaseal.yaml
   profile: local
   key_path: /path/to/new-signing-key.pem
   cert_path: /path/to/new-cert.pem
   tsa_url: https://freetsa.org/tsr
   merkle_db: ~/.novafabric/merkle.db
   ```
3. Distribute the new public key to all verifiers.
4. Backdate the old public key's effective expiry in your key registry — capsules
   signed with the old key remain verifiable using the old public key.
5. **Do not delete the old private key** until all capsules signed with it have
   been verified and archived.  Archive it encrypted in cold storage.
6. Emit a key rotation event in a transparency log — **future design**. There
   is no Rekor integration for key-rotation events. A related but distinct
   primitive *is* shipped: `trust/novaseal/witness.py` (ADR-0097,
   experimental) implements C2SP-style checkpoint + witness cosigning
   (anti-split-view) over the existing Merkle log — it is a library API only,
   not wired to any `nova` CLI command, and does not itself publish anywhere;
   it is not a substitute for the Rekor-based rotation event described here.

---

## 4. Compromise Recovery

If a private key is suspected or confirmed compromised:

1. **Immediately** revoke the key in your organisation's key registry / KMS.
2. Notify all capsule consumers that capsules signed with the compromised key
   may not be trustworthy.
3. Identify the time window of potential compromise.
4. For capsules in the affected window: re-seal using a new key if the source
   data is still available.
5. Preserve the compromised key's public key for forensic verification of
   pre-compromise capsules.
6. Generate a new key (§2) and update all signing profiles (§3).
7. File a security incident report per your organisation's IR procedure.
8. If the key was stored in a cloud KMS, follow the cloud provider's compromise
   response playbook (GCP: "Key version destruction"; AWS: "Immediate disable +
   schedule deletion").

---

## 5. Multi-Region Availability

For production deployments requiring high availability of the signing service:

### Option A: HSM cluster (on-premises)

Deploy YubiHSM 2 devices in each availability zone or region.  Use the YubiHSM
network daemon (`yhsmd`) with a shared key store backed by a replicated HSM cluster.
Ensure all nodes hold the same key material (synchronised at generation time).

### Option B: Cloud KMS with global replication

**GCP:**
```bash
gcloud kms keys create novaseal-production \
  --location global \
  --keyring novafabric-global \
  --purpose asymmetric-signing \
  --default-algorithm ec-sign-p256-sha256
```
GCP global keyrings replicate across all regions automatically.

**AWS:**
Use AWS KMS with multi-region keys:
```bash
aws kms create-key \
  --key-spec ECC_NIST_P256 \
  --key-usage SIGN_VERIFY \
  --multi-region
```
(`AwsKmsSigningBackend` always signs with `SigningAlgorithm=ECDSA_SHA_256`,
so the key must be `ECC_NIST_P256` — there is no Ed25519 option on this
path, not because of an AWS KMS limitation but because NovaSeal's AWS
backend only ever requests ECDSA_SHA_256.)

### Option C: Software keys with encrypted replication

For air-gapped or cost-sensitive deployments, replicate encrypted private key
files across availability zones using an encrypted object store (e.g.
S3 server-side encryption with KMS-managed keys).  Ensure ACLs limit access
to the NovaSeal signing service identity only.

---

## 6. PII Pepper Management (NOVA_PII_PEPPER)

The `NOVA_PII_PEPPER` environment variable is used by the PII Detection Gate
(cap-001) for HMAC-SHA256 subject anonymisation.  It is **NOT** a signing key
but must be treated with equivalent care.

**Rules:**
- Generate with: `python -c "import secrets; print(secrets.token_hex(32))"`
- Store in your secrets manager (Vault, AWS Secrets Manager, GCP Secret Manager).
- **NEVER** commit to version control, config files, or capsule data.
- Rotate on the same schedule as signing keys.
- All environments (dev, staging, production) must use different pepper values.

---

## 7. Verification Key Distribution

Distribute the public verification key through a trusted channel:
- Publish to the Rekor transparency log with a signed checkpoint — **future
  design** for the `local`/`aws_kms`/`azure_kv`/`gcp_kms` profiles (no such
  publishing exists today). If you use the **Sigstore keyless** path (§2.4)
  instead, Rekor inclusion already happens as part of every signature — no
  separate distribution step is needed for that path.
- Include in the organisation's `novaseal-trust-bundle.json` — **future
  design**; no such file format is implemented. For the `x509` profile (§2.3)
  the equivalent today is your own `pinned_fingerprints` set passed to
  `verify_x509_signature`.
- Distribute out-of-band (e.g. signed email, secure file share) for air-gapped
  deployments.

Verifiers must pin the specific public key they trust — do not use a global
"trust any NovaSeal key" policy.

---

## 8. References

- [Ed25519 specification (RFC 8032)](https://www.rfc-editor.org/rfc/rfc8032)
- [DSSE specification (Sigstore)](https://github.com/secure-systems-lab/dsse)
- [RFC 3161 — Time-Stamp Protocol](https://www.rfc-editor.org/rfc/rfc3161)
- [YubiHSM 2 documentation](https://developers.yubico.com/YubiHSM2/)
- [GCP Cloud KMS](https://cloud.google.com/kms/docs)
- [AWS KMS multi-region keys](https://docs.aws.amazon.com/kms/latest/developerguide/multi-region-keys-overview.html)
- ADR-0041: NovaSeal cryptographic core adoption
- ADR-0055: Dual-Mode Signing Identity (x509 cert-pinned offline slice shipped;
  sigstore profile + CA-bundle path validation remain future design)
- ADR-0058: Maker-checker dual-approval with NovaSeal
- ADR-0059: Linked-envelope chain maker-checker
- ADR-0071: Sigstore keyless signing
- ADR-0097: Verifiable transparency log — checkpoint + witness cosigning
- ADR-0185: Envelope encryption / cloud KMS key wrapping

## Backups and key material (ADR-0216 D4)

`nova backup create` excludes signing keys **by default** — data backups may
spread widely; key backups must not. A full disaster-recovery set can opt in
with `nova backup create --include-keys` (packs the signing keyring and
`novaseal.yaml` + its key/cert PEMs under `external/…`, flagged
`key_material`), and restoring them requires the second opt-in
`nova restore --restore-keys`. A set created with `--include-keys` must be
handled under the custody rules in this document — encrypted cold storage,
restricted access — never alongside ordinary data backups. The default
posture is unchanged: keys are backed up separately per this procedure, and a
restore without keys can verify everything but sign nothing.
