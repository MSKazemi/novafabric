# Encryption at Rest (opt-in envelope encryption)

This guide covers NovaFabric's **optional** application-layer envelope
encryption for capsule payloads in the object capsule store
([ADR-0185](../decisions.md)).

**Status: experimental** (v0.61, shipped 2026-07-16) — and **never the
default**. Out of the box, NovaFabric's at-rest posture is *integrity, not
confidentiality*: DSSE envelopes, Merkle logs, and WORM Object Lock make
evidence tamper-evident, while confidentiality is delegated to disk, database,
and bucket encryption. That remains the recommended default. Turn this feature
on only when you need application-held confidentiality on top — and understand
the trade first:

> **Lose the KEK, lose the evidence.** Envelope encryption trades
> confidentiality for a key-availability risk. A destroyed or unreachable key
> makes every encrypted capsule permanently unreadable — inside its WORM
> retention window, with no recovery path. Fold the KEK into your backup and
> DR posture (see the [backup & restore runbook](backup-restore.md)) before
> enabling this in anything you care about.

---

## 1. How it works

Per-object envelope scheme (`src/novafabric/trust/envelope_encryption.py`):

1. Every `put` generates a **fresh random 256-bit DEK** (data-encryption key)
   and encrypts the payload with **AES-256-GCM** (96-bit random nonce), using
   the existing `cryptography` dependency — no new dependency. Two
   encryptions of identical plaintext always produce distinct ciphertexts and
   distinct wrapped DEKs.
2. The DEK is **wrapped by a KEK** (key-encryption key) held by the
   operator's key backend, via the additive `KeyWrappingBackend` capability
   (`wrap_key` / `unwrap_key` / `kek_ref`). The wrapped DEK travels inside
   the stored envelope; the KEK never does.
3. **Encrypt-before-WORM (normative ordering).** The serialized ciphertext
   envelope is what the WORM adapter stores; the SHA-256 handed to the
   backend (checksum header / CAS gate) is recomputed **over the stored
   encrypted bytes**. `content_sha256` inside the envelope is likewise
   computed over the ciphertext — so integrity verification (hashes, Merkle
   leaves, WORM conformance) **never requires decryption or KMS access**.
4. **Reads are transparent.** The store detects the envelope by its schema
   marker fields and decrypts; objects written *before* encryption was
   enabled pass through unchanged, so mixed stores keep working.
5. **Chain-log objects are never encrypted.** They are integrity metadata,
   not capsule payloads — exactly as ADR-0031 excludes them from WORM.

Tampering is loud, with named exceptions: a ciphertext that fails its
recorded hash raises `CiphertextIntegrityError` *before* any key material is
touched; a tampered wrapped key raises `DekUnwrapError`; AES-GCM
authentication failure raises `BlobAuthenticationError`.

## 2. Enabling it (store wiring)

Opt-in requires **both** environment variables on the process that owns the
object capsule store:

```bash
# 256-bit KEK: raw 32 bytes, or 64 hex characters, in a local file
head -c 32 /dev/urandom > /secure/nova-kek.bin
chmod 600 /secure/nova-kek.bin

export NOVA_OBJECT_STORE_ENCRYPTION=1        # truthy: 1/true/yes/on
export NOVA_OBJECT_STORE_KEK_PATH=/secure/nova-kek.bin
```

With both set, `make_adapter` wraps whichever WORM backend you configured
(`s3`, `minio`, `ceph_rgw`, `azure_blob`, `local`) in the encrypting adapter,
and every capsule payload is envelope-encrypted before the WORM write. Absent
that configuration, behavior is **byte-for-byte unchanged**.

The local-file KEK path uses the NovaSeal `LocalSigningBackend` wrap
capability — suitable for dev/test parity and self-managed deployments where
you control the file's lifecycle.

### Per-tenant KEKs (ADR-0243, experimental)

Optionally, each tenant's capsules can wrap their DEKs under that tenant's
**own** KEK instead of the shared one — key compromise becomes tenant-scoped,
and removing one tenant's KEK makes exactly that tenant's data
cryptographically unreadable (the offboarding/erasure semantic, enforced
fail-closed with an error naming the tenant):

```bash
mkdir /secure/tenant-keks
head -c 32 /dev/urandom > /secure/tenant-keks/acme.kek     # one file per tenant
export NOVA_OBJECT_STORE_TENANT_KEK_DIR=/secure/tenant-keks
```

Tenants **without** a `<tenant>.kek` file keep using the default KEK — zero
change for existing deployments — and every pre-existing envelope stays
readable forever (the `tenant_key_id` field is additive). The registry
resolves tenants from the `capsules/{tenant}/…` key layout the store already
uses. Customer-held cloud KMS keys (BYOK), rotation campaigns, and the
maker-checker `tenant-keys shred` command are later ADR-0243 slices —
**planned**, not implemented.

## 3. Crypto-shred

Deleting the one wrapped DEK erases an object cryptographically while the
ciphertext stays untouched inside its WORM retention window — and still
verifies via `content_sha256`. This is the single-key-deletion semantic that
composes with the retention scheduler (ADR-0134): "deletion is evidence"
holds even for content that WORM will not let you physically remove.

A shredded envelope is permanently unrecoverable; reads raise the named
`ShreddedBlobError` rather than returning garbage. Shredding is idempotent.
Today `shred()` is a Python API
(`novafabric.trust.envelope_encryption.shred`); there is no CLI command for
it yet.

## 4. Honest limits — read before production

- **Corrected 2026-07-30 — cloud-KMS wrap paths are implemented, not
  planned.** Earlier drafts of this doc said the AWS KMS / Azure Key Vault /
  GCP KMS wrap paths were "infra-gated and not implemented" — that was stale.
  `AwsKmsWrappingBackend`, `AzureKvWrappingBackend`, and `GcpKmsWrappingBackend`
  (`src/novafabric/trust/novaseal/signing_backend.py`) all implement the
  `KeyWrappingBackend` capability this module calls, gated behind the
  `novafabric[seal-aws]` / `novafabric[seal-azure]` / `novafabric[seal-gcp]`
  extras. What works today, in order of maturity: the local 256-bit KEK file
  (production-ready), the test-only `MockKmsBackend`, and the three cloud
  wrapping backends (unit-tested against in-memory fakes of each SDK — 31
  passing tests across `tests/seal/test_aws_kms_wrapping.py`,
  `test_cloud_kms_backends.py`, `test_cloud_kms_wrapping.py`). Handing a
  non-wrap-capable backend to the crypto layer still raises
  `NotImplementedError` — it never silently downgrades.
- **Verified against mock/local KMS and in-memory cloud-SDK fakes.** The test
  suite proves the scheme (round-trip, tamper detection, shred,
  WORM-conformance-over-ciphertext, mixed-store reads) against the local-file
  KEK, the mock backend, and the three cloud wrapping backends' fake
  transports. **What has not been done**: end-to-end verification against a
  *live* AWS KMS / Azure Key Vault / GCP KMS endpoint with real credentials —
  that remains the one outstanding gap before recommending cloud KMS in
  production.
- **The §2 env-var wiring is local-KEK-only, by design, today.**
  `backend_router.make_adapter()` — the function §2's `NOVA_OBJECT_STORE_*`
  env vars drive — only ever constructs a `LocalSigningBackend` from
  `NOVA_OBJECT_STORE_KEK_PATH`; there is no env var that selects one of the
  three cloud wrapping backends for the *object-store* encryption feature.
  Using a cloud KMS to wrap the object-store DEK today means constructing
  `EncryptingAdapter(adapter, AwsKmsWrappingBackend(...))` (or the Azure/GCP
  equivalent) yourself in Python, bypassing `make_adapter`'s env-var
  convenience path — this is a real gap in the operator-facing surface, not
  a doc-only omission.
- **Security-Architect review pending.** Per the ADR's acceptance record,
  this feature requires a Security Architect sign-off before production use
  in regulated deployments. That review has not happened yet.
- **Scope is the object capsule store.** The SQLite/Postgres metadata store,
  the local `~/.novafabric/capsules/` filesystem store, and lineage data are
  not covered by this feature — use disk/database encryption there.
- **Confidentiality, not access control.** Anyone with the KEK file and store
  access can decrypt. Pair with the server's RBAC/tenancy controls.

## 5. Operational checklist

- [ ] KEK file generated from a real CSPRNG, mode `0600`, owned by the
      service user.
- [ ] KEK included in the backup/DR inventory ([backup-restore](backup-restore.md));
      restore drill performed with an encrypted object.
- [ ] Both env vars set in the service unit / container spec of every process
      that writes to or reads from the object store (a reader without the KEK
      gets `NotImplementedError`/unwrap failures, not plaintext).
- [ ] Documented owner for the KEK lifecycle (rotation is manual today —
      new writes use the new KEK; old envelopes still need the old KEK).

---

## See also

- [NovaSeal key management](../novaseal-key-management.md) — the operator
  key-management guide this feature extends
- [Backup & restore runbook](backup-restore.md) — where the KEK availability
  trade lands
- [ADR-0185](../decisions.md) —
  the full decision record, including the encrypt-before-WORM invariant and
  the infra-gated cloud-KMS plan
