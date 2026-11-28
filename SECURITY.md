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

- CI runs [pip-audit](https://pypi.org/project/pip-audit/) against every
  tracked Python lockfile (the root `uv.lock` and
  `examples/plugin-hook-reference/uv.lock`) on every pull request and weekly;
  HIGH/CRITICAL findings block merges.
- CI runs `npm audit` against every tracked npm lockfile (`ui/dashboard/`,
  `packages/nova-dashboard/`, `packages/nova-sdk-ts/`) on lockfile changes
  and weekly, behind the same severity gate; HIGH/CRITICAL advisories fail the job.
- Release container images are scanned with trivy; a CRITICAL finding with an
  available fix fails the publish workflow. The scan runs **after** the image
  has been pushed and signed, so a failed scan leaves that tagged image in the
  registry: it flags the release, it does not keep the image out.
- The pip-audit and npm-audit jobs report their result on the pull request.
  `main` has no required-status-check rule, so a failing audit stops a merge
  by maintainer practice, not by a repository setting.
- Exceptions go through the checked-in waiver files
  [`.pip-audit-waivers.toml`](.pip-audit-waivers.toml) and
  [`.npm-audit-waivers.toml`](.npm-audit-waivers.toml) — one schema, parsed
  by the same code: every waiver carries the vulnerability id, a written
  justification, and an **expiry date**. An expired waiver fails CI by
  construction, so accepted risk is always time-boxed and re-reviewed —
  waivers cannot rot into permanence.

## Supported versions

NovaFabric is pre-1.0 and releases frequently (v0.107.0, v0.108.0 and v0.109.0
shipped between 2026-10-23 and 2026-11-22). Given that cadence, only the latest tagged
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

- **Server mode** — two HTTP surfaces, neither started by any other command.
  Both bind `127.0.0.1` by default (works today):
  - `nova serve` (the dashboard and its local API, `127.0.0.1:4321`) requires a
    bearer token on every route except an explicit public list: the session
    token (`NOVAFABRIC_SERVE_TOKEN`, else `$NOVAFABRIC_HOME/.serve-token`, mode
    `0600`, kept after the server exits) holds `admin`; tokens you issue from
    it are stored as SHA-256 digests and default to `admin` scope unless you
    name a narrower one (`read`, `operate`, `audit`). It refuses a
    non-loopback `--host` (exit 2) unless the hidden `--insecure` flag is
    given. `--insecure` lifts only that refusal: authentication stays on, and
    a DNS-rebinding guard still answers `403` to any request whose `Host`
    header is not `localhost`, `127.0.0.1` or `[::1]` — a reverse proxy in
    front of it has to send one of those. Put TLS in front of any
    non-loopback bind.
  - `nova server` (the multi-user REST API, `127.0.0.1:7433`) authenticates
    with OIDC bearer JWTs (asymmetric algorithms only; `exp`, `iss` and `aud`
    required) when `issuer_url` and `audience` are configured, and otherwise
    with a generated local admin token (`$NOVAFABRIC_HOME/.server-token`,
    `0600`). API keys (`nvfk_…`, stored as SHA-256 of the secret) are accepted
    in either mode, and Ed25519 offline tokens too once an offline key is
    configured. Roles are `reader < writer <
    admin` plus the separate `auditor` (RBAC, experimental). It binds a
    non-loopback host with authentication on; anonymous access needs the
    escape hatches below.
- **Outbound lifecycle webhooks** (`NOVA_EVENTS_WEBHOOK`) — no default
  destination. Each event's subject carries refs and digests; the free-form
  `payload` is not limited by schema, and every record is secret-scanned
  before emission. HMAC signing (`NOVA_EVENTS_SIGN_SECRET`) is optional and
  recommended.
- **Other outbound network paths** — each is off until you configure it;
  NovaFabric has no telemetry and no update check: alert and
  bypass-notification webhooks (`NOVA_ALERTS_WEBHOOK`,
  `NOVA_BYPASS_NOTIFY_WEBHOOK`), the `nova server` webhook registry, RFC 3161
  timestamping (NovaSeal `tsa_url`; `nova export-evidence --timestamp`, which
  uses the public `freetsa.org` TSA when you name none), OIDC discovery and
  JWKS fetches, cloud KMS signing, S3, Rekor, OpenLineage, ClickHouse, NATS,
  an LLM judge endpoint, and self-tracing (loopback targets only unless
  `NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE` is set).
- **SCIM 2.0 provisioning** (`/scim/v2`) — inbound, active only when both the
  config flag and the dedicated `NOVAFABRIC_SCIM_TOKEN` are set; otherwise 404.
- **SAML endpoints** (`/v0/auth/saml/*`) — **experimental, assertion
  verification only.** Every SAML endpoint, SP metadata included, answers 404
  unless a `server.saml` block sets `enabled: true`; with it, the SP metadata
  is served read-only and unauthenticated. Login and ACS answer 501 unless the
  operator also sets `server.saml.experimental_acs_enabled: true` (the
  ADR-0138 D5 license gate cleared in v0.73.0 via the Tier-A `signxml`
  library). With that opt-in, the ACS verifies the assertion's signature
  (never skipped) and its conditions, then still answers 501
  `saml_session_unavailable`: no session or token is issued for a SAML login,
  so **SAML cannot authenticate a request today** — use OIDC, an API key or
  an offline token. Security-Architect review is a recorded pre-production
  blocking condition regardless of the opt-in flag.
- **OTLP GenAI ingest** (`POST /api/otlp/v1/traces`, on `nova serve` only) —
  needs a token with `operate` scope; foreign span data is secret-scanned at
  write time, the manifest is redacted, and the finished capsule gets the same
  residual pass, `evidence_digests` and opt-in seal as `nova capture` (since
  v0.105.0). Ingested capsules stay labeled `capture_level: ingested-otlp`.
- **Remote runners** (`nova capture --runner {docker,kubernetes,slurm,lsf,pbs}`)
  — send the workload to a container, cluster or batch scheduler. They forward a
  **default-deny allowlist** of environment variables (`NOVAFABRIC_*` except
  credential-shaped names such as `NOVAFABRIC_SERVER_TOKEN` or `NOVAFABRIC_POSTGRES_DSN`,
  plus `PATH` for `slurm` and any explicit `extra_env`), never the submitting shell's
  environment (ADR-0270). Since v0.106.0, `sbatch` and `bsub` themselves also run
  with a minimal environment (ADR-0311), because both copy their own environment
  into the job by default. `--runner local` is the deliberate exception: it runs
  as you, on your machine, inside the existing trust boundary.

  Fixed in this line: **B2** (disclosed 2026-08-28, fixed 2026-09-10) — the
  Kubernetes runner wrote every submitting environment variable into the `Job`
  object as a literal `value:`, readable with `get job` and persisted in etcd.
  If you ran `nova capture --runner kubernetes` on an affected version, treat any
  credential that was in that shell as exposed to anyone with read access to the
  namespace, and rotate it.

  Fixed in v0.106.0 (ADR-0311): with `--runner slurm` or `--runner lsf`, the
  job script was filtered but `sbatch` / `bsub` copied the submitting shell's
  whole environment into the job, as both do by default. If you used either
  runner on v0.105.0 or earlier, treat any credential that was in that shell as
  readable by the job and by anyone who can read its environment on the
  cluster, and rotate it. Workloads that need a variable now get it through
  `extra_env`. Also fixed in v0.106.0: every off-host runner (`docker`,
  `kubernetes`, `slurm`, `lsf`) forwarded `NOVAFABRIC_*` credentials such as
  `NOVAFABRIC_SERVER_TOKEN`, `NOVAFABRIC_API_KEY` or a `NOVAFABRIC_*_DSN`, and
  SLURM wrote them into the job script on `sbatch`'s command line; rotate any that
  were set when you used one of those runners.

  Fixed in v0.107.0: `--runner slurm` passed its job script — `extra_env`
  values included — as `sbatch --wrap <script>`, readable by every user on the
  submit host through `ps`; `--runner lsf` wrote the same values to
  `nova-<run>.lsf` in the capsule directory, where the capsule sealed it; and
  `--runner docker` passed `-e NAME=VALUE`. If you passed a credential through
  `extra_env` with one of these runners, rotate it. The value given on `nova
  capture`'s own command line (`--runner-option extra_env=K=V`) is still in that
  process's argv for the run, and Docker values remain visible to `docker
  inspect`.

  Also fixed in v0.107.0, in the opt-in server and export surfaces:
  a symbolic link inside a capsule made `nova export-evidence` and other
  exporters copy its target (any file the exporting user could read) into the
  output, and let a read-scope `nova serve` token read it — **inspect bundles,
  backups and exports made from capsules that contained links**; `nova serve`
  wrote the first 8 characters of its token into the audit log and SIEM export —
  **rotate the serve token if those records left the machine**; an
  `operate`-scope token could read or write files anywhere the server could;
  and the cluster collector served unauthenticated callers on every interface
  when no `NOVA_COLLECTOR_TOKEN` was set (it now serves only loopback,
  non-proxied requests — set a token whenever anything proxies to it).

  Also fixed in v0.107.0: a spec could register an asset directly into
  `production` (CLI, `nova serve` `operate` scope, `nova server` writer role),
  bypassing every promotion gate; reusing a version number after `unregister`
  let new content inherit rollback evidence; a read-scope `nova serve` caller
  could read files outside capsules through a link in a sibling capsule or a
  linked capsule root; and an `operate` credential could release a legal hold.
  If you run `nova serve` or `nova server` with non-admin credentials, review the
  registry for assets in `production` with no promotion history.

  Also fixed in v0.107.0, in capture and verification: with
  `--runner docker` or Kubernetes, links a containerised workload planted in its
  capsule directory could make host-side finalisation write host files (the
  secret scanner, `capsule.yaml`, `env.lock`, health and proof files); a capsule
  whose `capsule.yaml` was swapped for an edited `capsule.json` passed `nova
  verify` and the trust radar; the radar and run-verify API could show a tampered
  capsule as attested; `POST /api/evidence/{id}/verify` accepted tampered or
  re-signed bundles; and a Postgres `merkle_db` DSN was written to disk as a
  directory name, password included. **Re-verify capsules and bundles you relied
  on from these surfaces, rotate a Postgres password you put in `merkle_db`, and
  delete any stray `postgres:`/`postgresql:` directory where you ran `nova`.**

  Also fixed in v0.107.0: with `--pii-subject`, a workload could make
  capture overwrite a host file through a planted `redaction_manifest.json` link
  or a hard link; links and FIFOs nested below the capsule root survived capture
  and passed `nova verify` and `nova validate`; concurrent appends could fork the
  audit log's hash chain; and Evidence Bundle members no signature covered
  (`README.md`, the bundled schemas, `verifier_instructions`) could be rewritten
  without failing verification. **Re-run `nova validate`/`nova verify` on capsules
  captured from untrusted workloads, and treat Evidence Bundles exported before
  v0.107.0 as "signed content only".**

  **High, fixed in v0.108.0.** A second line-by-line code review (about 280 findings) found, among
  others: in `nova serve` ≤ v0.107.0, `GET /api/storage/validate` called S3 on a caller-chosen
  `endpoint_url` with the server's AWS credentials, and a capsule whose recorded command was HTML ran
  script in the Reports tab (stored XSS); `dek.db` (the raw per-subject PII keys), the erasure queue
  and the audit log were created at the umask mode (commonly world-readable) and the token files
  followed a symbolic link planted at their name; a revoked approver could still approve through an old
  policy version; the collector's `novaseal_batch_signer` forwarded every batch **unsigned**, also with
  `fail_open: false`; and `nova pii erase` wrote an unkeyed `sha256(subject_id)` into the audit log
  (dictionary-reversible for an email address). **Upgrade; then `chmod 0600` an existing audit log
  (`$NOVAFABRIC_HOME`) and any `dek.db`/token file you did not let v0.108.0 narrow on open, and treat
  erasure entries written before the upgrade as still carrying the old digest (the log is append-only).**
  The dashboard fixes take effect in the shipped bundle of this release. Full list: CHANGELOG
  `[0.108.0]`.

  **Critical, fixed in v0.107.0.** In `nova server` ≤ v0.106.1 a principal with the
  `writer` role could overwrite any file the server process could write through
  `POST /v0/evidence` (`output_path`, or an absolute/`..` `run_id`), and
  auto-generated evidence signing keys were world-readable (0664). In `nova serve`
  ≤ v0.106.1 any non-admin issued token could obtain the admin session token by
  posting Rego that read `opa.runtime().env` to `/api/policy/check` (the default
  single-token laptop setup is already `admin`). **Upgrade; then rotate the serve
  token and any non-admin issued tokens, rotate an auto-generated
  `$NOVAFABRIC_HOME/keys/signing_key.pem`, and check server-writable files if
  untrusted writers had access.** Known limits: audit-log locking uses
  advisory `flock`, which NFS without lock support and Lustre without `-o flock`
  do not honour; the audit chain is unkeyed.

If you find a way to make any disabled-by-default surface reachable without
explicit opt-in, that is a vulnerability — please report it.

**Capsule secret scanning is pattern-based.** Every file in a Run Capsule is
scanned and matches are redacted before the capsule is digested and sealed, and
`redaction-proof.json` records what was scanned and redacted. That includes
`otel-genai-spans.json` from `nova capture --emit-otel-genai`, which is written
before the residual pass and the digest step, so it is scanned and covered by
the seal like every other capture file (its message content is exported only
with `--capture-content`, through that path's own redaction first). Files that
other commands add to a capsule after capture are not covered by its seal; see
[docs/architecture/run-capsule.md](docs/architecture/run-capsule.md). It does **not**
prove that a capsule holds no secret: formats with no rule (PEM private keys,
JWTs, passwords, connection-string credentials, Slack/Stripe/Google/Azure keys,
an AWS secret key with no key name next to it, personal data) pass through. The
full list is in
[docs/architecture/run-capsule.md](docs/architecture/run-capsule.md#what-the-scanner-detects-and-what-it-does-not).
A secret in a format the pack *claims* to detect that survives into a finished
capsule is a vulnerability — please report it. A format on the "not detected"
list is a feature request.

**What `nova verify` proves, and what it does not (works today).**

- **Sealing is opt-in.** A capsule is sealed only once you run `nova seal init`
  (or configure `novaseal.yaml`); a default capture is unsealed. `nova verify`
  on an unsealed capsule exits 1 ("not sealed"): such a capsule carries no
  integrity evidence beyond its own files.
- **The signer is self-asserted unless you supply an anchor.** The signature is
  checked against the key or certificate carried in the envelope. Without a CA
  bundle (`--ca-bundle`, or `ca_bundle` in `novaseal.yaml`) `nova verify` exits 0
  and reports `identity_trust: self-asserted`: whoever held that key signed, and
  nothing binds the key to a person, organisation or machine. `nova verify
  --strict` exits 1 in that case. The same holds for an RFC 3161 timestamp
  without `--tsa-ca-bundle` (`timestamp_trust: unanchored`), see
  [Trust surfaces](docs/trust-surfaces.md#timestamp-trust-anchored-unanchored-absent-invalid).
- **What the seal covers.** The signed payload is the manifest (`capsule.yaml`),
  including `evidence_digests`: a SHA-256 of every regular file present when the
  capsule was sealed, apart from `.seal/` and `capsule.yaml` itself, which is
  bound by comparison with the signed payload. A changed or missing digested
  file, a substituted manifest, a symbolic link or special file, or a new file
  that a signed `*_ref` field names fails verification.
- **What it does not cover.** A file added after sealing that nothing signed
  names is listed as "not covered by the seal" and does **not** fail `nova
  verify`; do not rely on such a file. A seal shows the capsule is unchanged
  since it was sealed by that key, not that what the capsule records is true.

**Local WORM locks fail closed (ADR-0318).** If a configured `worm.db` (`LocalWormAdapter`, dev/test-only) cannot be
read, no capsule is deleted: every governed delete path and `nova retention` refuse with `worm_state_unavailable`.
In v0.107.0 and earlier, an unreadable or truncated `worm.db` was treated as "no locks". This does not make the local
adapter tamper-proof. For compliance retention, use object-store locks (S3 Object Lock, Azure immutable blob, GCS
Bucket Lock).

### What a capsule stores: model responses (changed in v0.109.0)

A capsule is evidence of what an agent did, so by default it stores model prompts and
responses in `model-calls.jsonl` after the capture-time secret scan, at every capture
level (works today): `NOVA_CAPTURE_LEVEL` does not change what the SDK and wire hooks
record, and no setting stops prompts (request messages) from being stored. A
metadata-only `minimal` level is **future design**. It is not a digest-only record:
treat a capsule as you treat the conversation it captured. `--pii-subject`
(experimental) tokenises the personal data its detector finds; `--capture-content`
governs only the OpenTelemetry span export.

**Since v0.109.0 more response bodies are stored** (experimental, ADR-0324). A call made
through `httpx`/`httpx2` or `requests` to a host the URL registry classifies as a model
provider (for example Mistral, Cohere, Together, Hugging Face Inference, Gemini, Vertex AI,
or an OpenAI-compatible server you register) now stores its response body, the
completion text, at every capture level, as OpenAI responses already were. Ollama
(`/api/chat`, `/api/generate`) and AWS Bedrock `Converse` responses are stored as well
(ADR-0320, ADR-0323). This is what lets `nova replay --mode mocked` serve those calls.

To stop storing the HTTP-layer bodies, set `NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off` (`0`,
`false` and `no` also work). Those calls are then recorded without a response and a
strict replay refuses them (`ModelSurfaceNotServable`). The variable does not affect the
dedicated OpenAI, Anthropic, Ollama and Bedrock surfaces. Check what your capsules hold
before you share them, and see the CHANGELOG entry for 0.109.0 for the full list.

### Escape hatches — every safety default that an environment variable can turn off

These exist so an operator can make a deliberate, auditable exception. They were
**undocumented until 2026-09-10**, which is the part that mattered: a control you
cannot find is a control you cannot audit, and a deployment review that greps the
docs for its own risk surface would have missed them.

| Variable | Turns off | Consequence |
|---|---|---|
| `NOVAFABRIC_SERVER_INSECURE_NO_AUTH` | authentication | restores pre-ADR-0184 **anonymous admin** — every request is an administrator |
| `NOVAFABRIC_SERVER_I_KNOW_THIS_IS_PUBLIC` | the non-loopback refusal | this and the row above **combine**: `insecure_no_auth` on a non-loopback bind raises `InsecureBindError` unless this is also set. ADR-0184 makes anonymous admin on a network-reachable interface a *doubly*-explicit choice — this is the second half |
| `NOVAFABRIC_SERVE_PATH_ROOTS` | the `nova serve` path allowlist | Extra absolute directories, `:`-separated, that `nova serve` endpoints may read and write when a request names a path (evidence export, capsule migrate, policy sign/test, KG/lineage `capsule_path`, metadata rebuild). Without it, writes are confined to `$NOVAFABRIC_HOME/exports`, `$NOVAFABRIC_HOME/evidence` and `$NOVAFABRIC_EVIDENCE_DIR`, and reads also to `$NOVAFABRIC_HOME` and the capsule dir. This is an **allowlist**; the former denylist opt-out `NOVAFABRIC_SERVE_ALLOW_ANY_PATH` is no longer honoured. Reads never reach the server's own secrets (token stores, `.serve-token`, `.env`, `dek.db`, ratchet state, `config.yaml`, audit logs) nor, for source reads, key material |
| `NOVAFABRIC_SERVER_WEBHOOKS_ALLOW_INSECURE_URL` | webhook URL validation | permits insecure webhook destinations for the server webhook registry, an outbound path that leaves the trust boundary (the other outbound paths are listed under [Scope](#scope)) |
| `NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT` | the device-grant guard | enables local/testing scaffolding whose `/approve` endpoint is **unauthenticated**. The code's own comment says it is off by default "so no production deployment exposes an unauthenticated role-approval surface" |
| `NOVAFABRIC_ALLOW_SEALED_DELETE` | the sealed-capsule delete refusal | `DELETE /v0/capsules`, `bulk-delete` and `nova serve`'s `DELETE /api/runs` may delete a NovaSeal-sealed capsule (`.seal/` present), destroying sealed evidence. It never overrides a legal hold or WORM lock; deletions stay audited (experimental, ADR-0206) |

Each accepts `1`/`true`/`yes`/`on`.

Already documented elsewhere, listed here so this table is the complete set:
`NOVAFABRIC_ALLOW_SCHEMA_SKEW`, `NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE`,
`NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE`, and the `NOVA_BYPASS_NOTIFY_*`
family; and, re-checked 2026-11-27, four that were missing from this list:
`NOVAFABRIC_PROMOTE_INSECURE_SELF_SIGNED=1` (any self-signed promote signer is
accepted — development and tests only), `NOVAFABRIC_PROMOTE_BYPASS_AUDIT_OPTIONAL=1`
(promotion verification no longer requires a bypass's audit entry),
`NOVA_OBJECT_STORE_ALLOW_PLAINTEXT_READS` (an encrypted object store also returns
non-envelope, plaintext objects; each such read is logged) and
`NOVAFABRIC_SERVER_METRICS_EXEMPT` (`nova server`'s `/metrics` needs no
authentication).

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

No ChaCha20 or bespoke primitives are used. Two legacy hashes appear, neither
as a signature or integrity primitive of NovaFabric's own (re-checked
2026-11-27): MD5 is computed only as the `Content-MD5` upload checksum the Azure
immutable-blob WORM adapter must send (`object_capsule_store/worm/azure.py`; the
object is addressed and checked by SHA-256), and SHA-1 only to match an RFC 3161
token's ESSCertID v1 signer-certificate hash, which RFC 2634 defines as SHA-1
(`trust/novaseal/tsa_trust.py`). `nova verify` accepts ESSCertID v1 tokens; a
v2-only check exists as a parameter of the Python verifier but no CLI option
sets it.

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
