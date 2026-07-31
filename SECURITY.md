# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Use GitHub's [private vulnerability reporting](https://github.com/novafabric/novafabric/security/advisories/new) to report issues confidentially.

We will acknowledge reports within 5 business days and aim to release a fix within 30 days for confirmed vulnerabilities.

## Vulnerability response

The severity-tiered targets below refine the general promise above. NovaFabric
is a pre-1.0 open-source project maintained by a small team: these are
**best-effort commitments, not a contractual SLA**. They cover both privately
reported vulnerabilities (disclosure flow above — unchanged) and dependency
CVEs surfaced by our automated scanners.

Severity follows CVSS as assigned by the advisory source; triage may adjust it
for NovaFabric's actual exposure (e.g. a vulnerable code path we never call).

| Severity | Triage (acknowledge + assess) | Fix or mitigate |
|----------|-------------------------------|-----------------|
| Critical | 72 hours                      | 14 days         |
| High     | 7 days                        | 30 days         |
| Moderate | —                             | 90 days         |
| Low      | best effort                   | best effort     |

"Fix or mitigate" includes a documented workaround or a default-off toggle
when a complete fix needs longer than the window.

### Dependency scanning and waivers

- CI runs [pip-audit](https://pypi.org/project/pip-audit/) against the locked
  dependency set on every pull request and weekly; HIGH/CRITICAL findings
  block merges.
- Release container images are scanned with trivy; CRITICAL findings with an
  available fix block the release.
- Exceptions go through the checked-in waiver file
  [`.pip-audit-waivers.toml`](.pip-audit-waivers.toml): every waiver carries
  the vulnerability id, a written justification, and an **expiry date**. An
  expired waiver fails CI by construction, so accepted risk is always
  time-boxed and re-reviewed — waivers cannot rot into permanence.

## Supported versions

NovaFabric is pre-1.0 and releases frequently (multiple tagged releases per
week during active development; latest tag as of this writing is **v0.94.0**).
Given that cadence, only the latest tagged release is supported — there is no
maintained LTS line before v1.0.

| Version         | Supported            |
|------------------|----------------------|
| 0.94.x (latest)  | Yes                  |
| < 0.94           | No — upgrade to latest |

This table will be replaced by a real support-window policy at the v1.0
freeze.

## Scope

NovaFabric is local-first: in its default configuration it is a self-contained
CLI tool with no network surface. The always-present attack surfaces are:

- YAML parsing (malicious spec files)
- SQLite database access (local filesystem)
- CLI argument handling

Several **opt-in** surfaces exist and are disabled by default; each has a
STRIDE analysis in [`THREAT_MODEL.md`](THREAT_MODEL.md):

- **Server mode** (`nova server` / `nova serve`) — network-exposed REST API
  (OIDC/RBAC or token auth).
- **Outbound lifecycle webhooks** (`NOVA_EVENTS_WEBHOOK`) — the only outbound
  network path; no default destination, payloads are IDs/digests only and
  secret-scanned before emission, HMAC signing is optional and recommended.
- **SCIM 2.0 provisioning** (`/scim/v2`) — inbound, active only when both the
  config flag and the dedicated `NOVAFABRIC_SCIM_TOKEN` are set; otherwise 404.
- **SAML endpoints** (`/v0/auth/saml/*`) — SP metadata is always available
  read-only. Assertion consumption (login + ACS) is refused (501) unless the
  operator explicitly sets `server.saml.experimental_acs_enabled: true` (the
  ADR-0138 D5 license gate cleared in v0.73.0 via the Tier-A `signxml`
  library); signature validation is never skipped, even when enabled. This
  path remains `experimental`, and Security-Architect review is a recorded
  pre-production blocking condition regardless of the opt-in flag.
- **OTLP GenAI ingest** (`POST /api/otlp/v1/traces`) — token-authenticated;
  foreign span data is secret-scanned at write time.

If you find a way to make any disabled-by-default surface reachable without
explicit opt-in, that is a vulnerability — please report it.

## FIPS 140-3 posture (ADR-0195 — accepted 2026-07-17)

**NovaFabric does not claim to be FIPS 140-3 validated or "FIPS compliant."**
FIPS 140-3 validation applies to cryptographic *modules*, not applications.
NovaFabric implements no cryptographic primitives of its own: every primitive
is delegated to the `cryptography` package (which calls OpenSSL) and stdlib
`hashlib`/`hmac` (which use OpenSSL where available). Whether a deployment
operates with a validated module is a property of the OpenSSL that deployment
links, plus the algorithm caveat below.

### Crypto inventory (verified against the tree, 2026-07-30)

| Primitive | Where used | FIPS approvability |
|---|---|---|
| Ed25519 | NovaSeal envelopes/ratchet (`trust/novaseal/`), trust keyring, offline tokens (`server/offline_tokens.py`), hybrid-signature envelope default algorithm (`trust/novaseal/hybrid_signature.py`, ADR-0072), did:key + Verifiable Credentials (`trust/did.py`, ADR-0075), delegation-chain grants (`trust/delegation.py`, ADR-0106), transparency-log witness cosigning (`trust/novaseal/witness.py`, ADR-0097), jurisdiction site-seals (`compliance/sovereignty.py`, ADR-0077) | Approved as an algorithm (FIPS 186-5); **module coverage caveat below** |
| ECDSA P-256 (DSSE) | NovaSeal signing backend, RFC 3161 verification, x509 certificate-pinned signing identity's EC path (`trust/novaseal/x509_identity.py`, ADR-0055) | Approved (FIPS 186-4/186-5) |
| RSA-PSS-SHA256 (2048+) | x509 certificate-pinned signing identity's RSA path (`trust/novaseal/x509_identity.py`, ADR-0055) — added v0.91.0 | Approved (FIPS 186-4/186-5, SP 800-56B for key sizes ≥2048) |
| AES-256-GCM | Envelope encryption at rest (ADR-0185), key wrapping, cloud-KMS DEK wrap/unwrap (AWS KMS / Azure Key Vault / GCP KMS backends, `trust/novaseal/signing_backend.py`) | Approved (SP 800-38D) |
| SHA-256 | Merkle trees (RFC 6962 evidence log + pairwise NovaSeal log — two incompatible constructions, do not mix), Merkle Mountain Range accumulator (`trust/novaseal/mmr.py`, ADR-0110 §NF-051), ledger/audit hash chains, CAS addressing, RFC 3161 | Approved (FIPS 180-4) |
| HMAC-SHA256 | Lifecycle-event/webhook signing (`events/signing.py`) | Approved (FIPS 198-1) |
| BLAKE3 (optional) | `storage/dual_object_store.py` acceleration, SHA-256 fallback exists | **Not approved** — leave the optional `blake3` package uninstalled in FIPS deployments |
| ML-DSA (post-quantum) | Registry slot only in the hybrid-signature envelope (`trust/novaseal/hybrid_signature.py`, ADR-0072 Phase 1) — **not implemented**; no Tier-A ML-DSA library is wired in, so the "hybrid" envelope today signs Ed25519 only | Not yet shipped — no approvability claim |

No MD5, SHA-1, ChaCha20, or bespoke primitives are used in security-relevant
paths.

### Operating with a validated module (documented intent — not a tested claim)

- The PyPI `cryptography` wheels statically link their **own, non-validated**
  OpenSSL. A FIPS deployment must build `cryptography` from source against the
  system OpenSSL 3.x with the FIPS provider installed and enabled; non-approved
  algorithms then fail at call time instead of silently degrading.
- Stdlib `hashlib`/`hmac` ride the same system OpenSSL in CPython builds linked
  against it.
- Do not install the optional `blake3` package (the SHA-256 fallback engages
  automatically).
- Verifying that the module actually runs in FIPS mode is a deployment
  responsibility; NovaFabric documents the recipe and certifies nothing.
  This recipe is **documented intent** until a FIPS-mode deployment has
  actually been exercised.

### The Ed25519 caveat, stated plainly

FIPS 186-5 (2023) approves EdDSA including Ed25519, but as of this writing the
widely deployed **validated** OpenSSL FIPS providers (3.0.x line) do not list
EdDSA among approved services — check the approved-algorithm list of your
specific validated module. Where the module lacks approved EdDSA:

- capsule signing has a FIPS-friendly profile — the **ECDSA P-256 DSSE** path;
- the **ed25519-only surfaces** (offline tokens, trust keyring, NovaSeal
  envelope/ratchet) have no P-256 alternative today and either operate
  non-approved or are unavailable in a strict FIPS deployment. This is a
  known, documented gap; algorithm agility for those surfaces would be its
  own ADR.
