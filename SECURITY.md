# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Use GitHub's [private vulnerability reporting](https://github.com/MSKazemi/novafabric/security/advisories/new) to report issues confidentially.

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
  Python dependency set on every pull request and weekly; HIGH/CRITICAL
  findings block merges.
- CI runs `npm audit` against every tracked npm lockfile (`web/`,
  `packages/nova-dashboard/`, `packages/nova-sdk-ts/`) on lockfile changes
  and weekly, behind the same severity gate; HIGH/CRITICAL advisories block.
- Release container images are scanned with trivy; CRITICAL findings with an
  available fix block the release.
- Exceptions go through the checked-in waiver files
  [`.pip-audit-waivers.toml`](.pip-audit-waivers.toml) and
  [`.npm-audit-waivers.toml`](.npm-audit-waivers.toml) — one schema, parsed
  by the same code: every waiver carries the vulnerability id, a written
  justification, and an **expiry date**. An expired waiver fails CI by
  construction, so accepted risk is always time-boxed and re-reviewed —
  waivers cannot rot into permanence.

## Supported versions

NovaFabric is pre-1.0 and releases frequently (multiple tagged releases per
week during active development). Given that cadence, only the latest tagged
release is supported — there is no maintained LTS line before v1.0. See the
[releases page](https://github.com/MSKazemi/novafabric/releases) for the
current version.

| Version           | Supported              |
|-------------------|------------------------|
| Latest tag        | Yes                    |
| Anything earlier  | No — upgrade to latest |

The full policy — what upgrading promises today, the runtime support matrix,
and the channel/LTS structure that takes effect at the v1.0 freeze — lives in
[`docs/support-policy.md`](docs/support-policy.md).

## Scope

NovaFabric is local-first: in its default configuration it is a self-contained
CLI tool with no network surface. The always-present attack surfaces are:

- YAML parsing (malicious spec files)
- SQLite database access (local filesystem)
- CLI argument handling

Several **opt-in** surfaces exist and are disabled by default; each has a
STRIDE analysis in the project's internal threat model:

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
- **Remote runners** (`nova capture --runner {docker,kubernetes,slurm,lsf,pbs}`)
  — send the workload to a container, cluster or batch scheduler. They forward a
  **default-deny allowlist** of environment variables (`NOVAFABRIC_*`, plus
  `PATH` for `slurm` and any explicit `extra_env`), never the submitting shell's
  environment (ADR-0270). `--runner local` is the deliberate exception: it runs
  as you, on your machine, inside the existing trust boundary.

  Fixed in this line: **B2** (disclosed 2026-08-28, fixed 2026-09-10) — the
  Kubernetes runner wrote every submitting environment variable into the `Job`
  object as a literal `value:`, readable with `get job` and persisted in etcd.
  If you ran `nova capture --runner kubernetes` on an affected version, treat any
  credential that was in that shell as exposed to anyone with read access to the
  namespace, and rotate it.

If you find a way to make any disabled-by-default surface reachable without
explicit opt-in, that is a vulnerability — please report it.

### Escape hatches — every safety default that an environment variable can turn off

These exist so an operator can make a deliberate, auditable exception. They were
**undocumented until 2026-09-10**, which is the part that mattered: a control you
cannot find is a control you cannot audit, and a deployment review that greps the
docs for its own risk surface would have missed all four.

| Variable | Turns off | Consequence |
|---|---|---|
| `NOVAFABRIC_SERVER_INSECURE_NO_AUTH` | authentication | restores pre-ADR-0184 **anonymous admin** — every request is an administrator |
| `NOVAFABRIC_SERVER_I_KNOW_THIS_IS_PUBLIC` | the non-loopback refusal | the two above **combine**: `insecure_no_auth` on a non-loopback bind raises `InsecureBindError` unless this is also set. ADR-0184 makes anonymous admin on a network-reachable interface a *doubly*-explicit choice — this is the second half |
| `NOVAFABRIC_SERVE_ALLOW_ANY_PATH` | the dashboard path denylist | `nova serve` endpoints that take a caller-chosen path (evidence export, capsule migrate, promote-sign) stop refusing system directories. Note this is a **denylist, deliberately not a sandbox** |
| `NOVAFABRIC_SERVER_WEBHOOKS_ALLOW_INSECURE_URL` | webhook URL validation | permits insecure webhook destinations; the outbound path is the only one that leaves the trust boundary |
| `NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT` | the device-grant guard | enables local/testing scaffolding whose `/approve` endpoint is **unauthenticated**. The code's own comment says it is off by default "so no production deployment exposes an unauthenticated role-approval surface" |

Each accepts `1`/`true`/`yes`/`on`.

Already documented elsewhere, listed here so this table is the complete set:
`NOVAFABRIC_ALLOW_SCHEMA_SKEW`, `NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE`,
`NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE`, and the `NOVA_BYPASS_NOTIFY_*`
family.

**If you operate a NovaFabric server, grep your deployment for these names.** A
set value is not a bug — it is a decision someone made, and it should appear in
your change record with a reason.

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
