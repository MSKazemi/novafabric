# Air-Gapped / Offline Deployment Guide

How to install and operate NovaFabric with **no internet connectivity** —
classified networks, HPC enclaves, regulated environments. Labels follow the
[docs honesty rule](../../CONTRIBUTING.md#documentation-status-labels): **works today**, **experimental**,
**planned**, or **future design**.

The short version: NovaFabric is **local-first by design**. Core local-mode
features — capture, validate, replay, diff, lineage on local capsules —
require no network, ever. The features that *can* reach out (TSA, Sigstore,
cloud WORM/KMS, OIDC) are all opt-in, all configurable to private endpoints,
and all have documented offline modes or honest "not usable offline" answers
(§5).

---

## 1. No telemetry, no phone-home — the guarantee

**Status: contract — accepted
[ADR-0189](../decisions.md).**

- NovaFabric emits **zero unsolicited network traffic**: no telemetry, no
  update checks, no license keys, no seat/entitlement checks, no feature
  kill-switches, no phone-home — ever. This is an accepted ADR, not a
  default you must disable.
- Every feature in the repository is usable in full, offline, under the
  project license. Where a feature is withheld (e.g. SAML assertion
  consumption returns 501), the gate is a dependency-*license* question
  (ADR-0024/ADR-0138 §D5), never an entitlement check.
- Self-tracing ([ADR-0182](../decisions.md)
  D5) is opt-in, default OFF, and can only target the deployment's **own**
  OTLP ingest or an operator-configured private endpoint — spans never leave
  the deployment.

An air-gap security review can therefore be answered in one line: there is
no call-home code path to firewall.

---

## 2. Offline package install (pip / uv wheel mirroring)

**Status: works today** (standard Python packaging — nothing
NovaFabric-specific is required).

On a connected staging machine, download wheels for your Python version and
platform:

```bash
# Pick the extras you actually deploy (see the extras table below)
pip download 'novafabric[server,serve]' -d ./wheelhouse \
    --python-version 3.12 --only-binary=:all:
```

Transfer `wheelhouse/` across the boundary, then install with the index
disabled:

```bash
pip install --no-index --find-links ./wheelhouse 'novafabric[server,serve]'
# uv equivalent:
uv pip install --no-index --find-links ./wheelhouse 'novafabric[server,serve]'
```

Alternatively, host the wheels on an internal mirror (devpi, Artifactory,
Nexus, `simple/` static index) and point `PIP_INDEX_URL` /
`UV_INDEX_URL` at it — NovaFabric hardcodes no registry.

Extras you may need to include in the mirror (from `pyproject.toml`;
Tier-A/B licensing per ADR-0024):

| Extra | Brings | Air-gap note |
|---|---|---|
| `server` | FastAPI, uvicorn, psycopg, alembic, PyJWT | Needed for `nova server`, offline tokens |
| `serve` | FastAPI, uvicorn, duckdb, pyarrow, python-louvain | Local dashboard (the topology extractor needs the last three) |
| `worm-s3` / `worm-azure` / `worm-gcs` | boto3 / azure-storage-blob / google-cloud-storage | Only if you run that WORM backend; S3-compatible works against in-network endpoints (§5) |
| `seal-aws` / `seal-azure` / `seal-gcp` | Cloud KMS SDKs | **Cloud KMS profiles need the cloud** — use the `local` seal profile offline |
| `seal-postgres` | psycopg | Postgres Merkle log at >1M entries |
| `sigstore` | sigstore | **Not usable air-gapped** (§5) — omit it |
| `scale` | duckdb, pyarrow, clickhouse-connect, nats-py, fastavro, pyiceberg, blake3, boto3 | Evidence Fabric scale tier |
| `query` | duckdb | Optional `nova query` accelerator; stdlib `sqlite3` fallback is always available |
| `clickhouse` | clickhouse-connect | ClickHouse cost attribution |
| `lineage-migration` | pyarrow | Lineage migration kit |
| `all` | every non-cloud-vendor, non-agent-framework extra | Escape hatch that restores pre-v0.99.0 *importability* — but it is a **superset** of the old default (it also pulls `compliance`, `spkg`, `scale`, `sigstore`, `janusgraph`…), so it installs more, not less. **Includes `sigstore`, which is unusable air-gapped** — prefer naming the narrow extras you actually need |
| `otlp`, `spkg`, `lineage-kuzu`, … | see `pyproject.toml` | As needed |

> **Changed in v0.99.0 — re-check your wheelhouse.** `duckdb`, `pyarrow`,
> `python-louvain` and `clickhouse-connect` (and, transitively, `numpy`) are no
> longer part of the default install; they moved to the extras above (ADR-0222).
> This makes an air-gapped mirror ~299 MB smaller — but if your existing
> `pip download` line names no extras and your deployment imports any of them,
> they will now be missing from the wheelhouse. Add the extra you need, or use
> `pip download 'novafabric[all]' -d ./wheelhouse` to mirror the previous
> surface. Core commands (`capture`, `validate`, `replay`, `diff`, `lineage`,
> `insights`, `query`) need none of them.
>
> Note that `[all]` also pulls in `sigstore`, which the table above tells you to
> omit from an air-gapped mirror (§5), and several other extras you may not
> want. For an enclave, prefer downloading the narrow extras you actually use —
> e.g. `pip download 'novafabric[server,serve,query]'` — over `[all]`.

Container images and the Helm chart ship from GHCR
(`ghcr.io/mskazemi/novafabric`, `oci://ghcr.io/mskazemi/charts/novafabric`
— [Server Deployment Guide](server-deployment.md)); mirror them into your
private registry (`docker pull` → `docker push registry.internal/...`) —
nothing in the chart requires the public registry at runtime.

---

## 3. Offline identity: ed25519 tokens, no IdP

**Status: experimental**
([ADR-0018](../decisions.md);
`src/novafabric/server/offline_tokens.py`). This is the intended auth mode
for air-gapped clusters, SLURM batch jobs, and CI without an OIDC provider.

- `nova server issue-token` generates an **ed25519 keypair** on first use
  (default `~/.novafabric/keys/offline-key.pem` plus `.pub`). The server
  verifies tokens with the public key **only when started with
  `NOVAFABRIC_OFFLINE_KEY_PATH=<key-dir>/offline-key.pub`** (environment only —
  `offline_key_path` in the YAML file is ignored). JWTs are signed **and
  verified locally** — no external endpoint is ever contacted.
- Issue: `nova server issue-token --subject worker-01 --roles reader,writer
  --expires-in 30d` (prints the JWT).
- Revoke: `nova server revoke-token <jti>`. Issue/revoke records live in a
  `token_audit` SQLite DB stored **adjacent to the key**
  (`<key-name>-tokens.db`); revoked tokens get 401 on the next call.
- Verification checks the ed25519 signature, the `exp` claim, and the
  revocation record — all local I/O.
- Keep the private key mode 0600 (it is generated that way); the server does
  not check the key file's permissions itself.

Since v0.61, local no-OIDC mode also has the auto-generated local bearer
token (`~/.novafabric/.server-token`,
[ADR-0184](../decisions.md)) —
also fully offline. Full setup:
[Server Deployment Guide — Scenario 4](server-deployment.md).

---

## 4. What works offline with zero configuration

**Status: works today** (surfaces individually labelled in their own docs;
most are experimental-maturity but network-free).

- `nova capture / validate / replay / diff / lineage` — the entire local
  core, by explicit project rule (core local-mode features must never
  require internet).
- NovaSeal **local profile** signing (`profile: local`, ECDSA P-256 key on
  disk) and `nova verify` — signature and Merkle-log checks are local I/O.
- Evidence Bundle verification — designed to be checked offline with
  `sha256sum` + an ed25519 verifier ([Concepts](../concepts.md)).
- `nova backup create` / `nova backup verify` / `nova restore` — backup
  verification explicitly requires "no live deployment, network, or private
  keys" ([Backup & Restore Runbook](backup-restore.md)).
- `nova support-bundle` — works in local mode and offline
  ([ADR-0187](../decisions.md)).
- Encryption at rest for the object store
  ([ADR-0185](../decisions.md))
  with a **local KEK file**:
  `NOVA_OBJECT_STORE_ENCRYPTION=1` + `NOVA_OBJECT_STORE_KEK_PATH=<256-bit
  key file>` — no KMS required (fails closed if the flag is set without a
  KEK path).

---

## 5. Features that reach the network — and their offline modes

| Feature | Needs network for | Offline mode / degradation |
|---|---|---|
| **RFC 3161 timestamps** (NovaSeal `tsa_url`) | HTTPS POST to the timestamp authority you configure — **opt-in since v0.103** (ADR-0292); no TSA is contacted unless `tsa_url`/`tsa_urls` is set | **Omit `tsa_url`** (the default; or set it `""`): NovaSeal still signs with ECDSA, just without a timestamp token and without any network call — `nova verify` then reports the timestamp check accordingly, which is expected, not tampering. Or run a **private TSA inside the enclave** and point `tsa_url` at it ([operator guide §5b](../operator-guide.md), [NovaSeal configuration](../novaseal-configuration.md)). The timestamp-verification code additionally has an explicit HPC air-gap mode (`offline_mode=True` skips nonce-store writes and all network calls — `src/novafabric/trust/novaseal/timestamp.py`) |
| **Sigstore keyless signing/verification** (`--backend sigstore`) | Fulcio + Rekor public infrastructure | **Not usable air-gapped.** Use the local ECDSA DSSE backend (the default). Don't mirror the `sigstore` extra into the enclave |
| **Cloud WORM stores** (S3/Azure/GCS) | The bucket endpoint | Any **S3-compatible store inside the network** works: every S3-family adapter takes a configurable `endpoint_url` (Ceph RGW and other S3-compatible OSS; `src/novafabric/object_capsule_store/worm/`), and the `NovaObjectStore` reads `NOVA_S3_ENDPOINT_URL` / `NOVA_S3_BUCKET` / `NOVA_S3_ACCESS_KEY` / `NOVA_S3_SECRET_KEY` (`src/novafabric/storage/nova_object_store.py`). Azure/GCS backends require their respective (possibly private/Stack) endpoints |
| **Cloud KMS seal profiles** (AWS KMS / Azure KV / GCP KMS) | The cloud KMS API | Use the **`local` seal profile** (file-based key) — the fully offline path; HSM options in [key management](../novaseal-key-management.md) |
| **OIDC login** | The IdP's issuer/JWKS endpoints | An **in-network IdP** (e.g. Keycloak inside the enclave) works — `issuer_url` is fully configurable. With no IdP at all, use **offline ed25519 tokens** (§3) |
| **OTel export** (`otel_endpoint`) | Your OTLP collector | Empty = disabled (the default). Point it only at an in-network collector |
| **`nova lineage emit-openlineage` HTTP mode** | Your OpenLineage endpoint | Optional; stdout/file modes are local |
| **Package/image updates** | PyPI/GHCR | Mirror per §2. There are **no runtime update checks** to disable (§1) |

Rule of thumb (and repo policy): everything callable is configurable to a
private endpoint, mirror, or proxy — no external service URL is hardcoded as
a requirement. If you find a counter-example, file it as a bug.

---

## 6. A minimal fully-offline server profile

**Status: experimental** (each piece labelled above).

```yaml
# /etc/novafabric/server.yaml — passed with --config; keys are top-level
backend: postgres            # in-network Postgres; or sqlite for single-host
host: "127.0.0.1"            # or your internal interface
port: 7433
# no oidc: block → OIDC off; secrets (DSN, offline key) come from the environment
```

```yaml
# ~/.novafabric/novaseal.yaml — sealing with no TSA
profile: local
key_path: ~/.novafabric/seal.key
cert_path: ~/.novafabric/seal.crt
# tsa_url omitted → ECDSA signing without RFC 3161 timestamps
merkle_db: ~/.novafabric/novaseal-merkle.db
```

```bash
export NOVAFABRIC_POSTGRES_DSN="postgresql://nova:***@pg.internal:5432/novafabric"
nova server issue-token --subject batch-runner --roles writer --expires-in 90d \
  --key-path /etc/novafabric/keys/offline-key.pem
export NOVAFABRIC_OFFLINE_KEY_PATH=/etc/novafabric/keys/offline-key.pub
nova server start --config /etc/novafabric/server.yaml --backend postgres
```

Verify the deployment end-to-end without leaving the enclave:

```bash
nova capture -- python agent.py       # capture
nova validate <capsule-dir>           # schema check
nova verify <capsule-dir>             # seal check (signature + Merkle log)
nova doctor --check-storage           # DB/migration state
nova support-bundle                   # diagnostics, offline-safe
```

---

## 7. Honest limitations in an air gap

- **No RFC 3161 trusted time** unless you operate a private TSA — sealed
  capsules then prove *integrity* and *log inclusion*, but third-party
  trusted timestamps are absent by configuration.
- **No Sigstore transparency-log evidence** — local-key DSSE only.
- **Public-CA TLS and revocation checking** are your PKI's problem, as in
  any enclave; NovaFabric's `tls:` block takes your internal cert/key paths.
- **Vulnerability-management cadence** (pip-audit/Dependabot,
  [ADR-0186](../decisions.md))
  runs in this repo's CI, not in your
  enclave — mirror updated wheels on your own schedule.
- Anything labelled **future design** elsewhere (federation, at-scale graph
  backends, …) is equally unimplemented offline — the air gap changes
  nothing about the honesty labels.


## 8. Signed air-gap bundle with a complete wheelhouse (ADR-0249)

**Status: experimental.** The bundle format, the closure builder and the
offline verifier work today and are exercised by the `airgap-bundle` CI job
on every pull request that touches them; no release publishes a bundle yet
(see *What is not built yet* below).

§2 is the plain-`pip` route. The bundle adds what an enclave review usually
asks for: one file that carries the NovaFabric wheel **and every pinned
dependency**, a signed inventory of it, and a verifier that runs **before
anything is installed** and refuses a bundle that is corrupted, incomplete or
padded.

### 8.1 Build it (connected machine)

You need a checkout of this repository, `uv`, a Python with `pip` for the
target Python version, and an ed25519 private key that stays on this machine:

```bash
# A signing key (once). Keep the private half off the transfer media.
(umask 077; openssl genpkey -algorithm ed25519 -out airgap-signing.pem)
openssl pkey -in airgap-signing.pem -pubout -out airgap-signing.pub.pem

uv build --wheel --out-dir dist
uv run python scripts/build_airgap_bundle.py \
    --out dist/novafabric-airgap.tar \
    --signing-key airgap-signing.pem \
    --closure-wheel dist/novafabric-*-py3-none-any.whl \
    --extras server,query \
    --pip-python /usr/bin/python3.12      # a Python with pip, matching the target
```

The dependency set comes from `uv.lock` (`uv export --locked`, so a lock that
no longer matches `pyproject.toml` is refused), and `pip download
--only-binary=:all: --require-hashes` checks every wheel against the hash in
the lock. Omit `--extras` for the core install. Set `SOURCE_DATE_EPOCH` to pin
the manifest timestamp.

What the bundle (a tar) contains:

| Member | Purpose |
|---|---|
| `wheels/` | The NovaFabric wheel and one wheel per pinned dependency |
| `requirements.txt` | Hashed pins for every dependency **and** the NovaFabric wheel itself, for `pip install --require-hashes` |
| `bundle-info.json` | NovaFabric version, extras, target Python version / platform tags, source commit, `uv.lock` SHA-256, `uv`/`pip` versions. No host name, user name or path |
| `SHA256SUMS` | The same digests in `sha256sum -c` format |
| `verify_airgap_bundle.py` | The offline verifier (standard library only) |
| `INSTALL.txt` | The commands below, for an enclave without these docs |
| `airgap-manifest.json` + `airgap-manifest.dsse.json` | The inventory (path, SHA-256, size of every member) and its DSSE ed25519 signature |

The builder refuses to add the signing key (or a byte-identical copy of it)
as a member.

### 8.2 Verify and install (isolated host)

Bring the **public** key across separately from the bundle (another medium,
or compare its SHA-256 fingerprint over a second channel); a key that travels
inside the bundle proves nothing.

```bash
tar -xf novafabric-airgap.tar verify_airgap_bundle.py
python3 verify_airgap_bundle.py --bundle novafabric-airgap.tar \
    --public-key airgap-signing.pub.pem --extract-to ./nova-airgap

python3 -m venv ./nova-venv
./nova-venv/bin/python -m pip install --no-index \
    --find-links ./nova-airgap/wheels --require-hashes \
    -r ./nova-airgap/requirements.txt
./nova-venv/bin/nova --version
```

`uv pip install` with the same flags works too; the `airgap-bundle` CI job
installs that way, because this repository's CI installs with `uv`.

The verifier needs only Python 3.12 and either the `cryptography` package or
the `openssl` command (OpenSSL 3.0 or later) for the ed25519 check; with
neither, it refuses rather than skipping the signature. It exits `0` only
when all of these hold, and otherwise names each problem and exits `1`:

- the manifest signature verifies with the key you passed, and the signed
  payload is byte-identical to the manifest in the tar;
- every listed member is present with the listed size and SHA-256;
- no member is present that the manifest does not list, no name appears twice,
  and every entry is a regular file (links and devices are refused);
- no path is absolute or contains `..`;
- `SHA256SUMS`, when present, agrees with the signed manifest line for line.

`--extract-to` writes the members into a fresh staging directory *while*
hashing them and renames it into place only after every check passed, so the
wheels you install are the bytes that were verified; on failure nothing is
left behind. `--integrity-only` (instead of `--public-key`) checks hashes
only: it catches corruption in transit, not a substituted bundle, and says so.

### 8.3 Limits

- **One target per bundle.** A wheelhouse is specific to one Python minor
  version, one platform (OS, CPU architecture, glibc/musl) and one
  implementation; `bundle-info.json` records which. By default it targets
  the `--pip-python` interpreter's own platform. `--python-version` and
  `--platform` are passed to `pip download` as-is, but pip uses them for
  wheel-tag and `Requires-Python` checks only; environment markers in the
  pinned requirements are still evaluated against the interpreter running
  pip. **Build on a host that matches the target.** CI covers Linux x86_64,
  CPython 3.12, core install only.
- **Wheels only.** A dependency that publishes no wheel cannot be bundled:
  today that is `python-louvain`, so the `serve` and `all` extras fail closed
  at build time, naming it. Mirror those per §2 or build that wheel yourself.
- **Python packages only.** The container image, the Helm chart, the docs site
  and a CVE advisory snapshot are not in the bundle yet (below); mirror the
  image and chart per §2.
- **Optional features that need a network stay unavailable** inside the
  enclave regardless of how NovaFabric was installed (§5).

### 8.4 Signing, and what is not built yet

The signature reuses the existing evidence signing stack (DSSE over ed25519,
`novafabric.export_blob.airgap`), so the trust anchor is whichever ed25519
key you sign with. NovaFabric's published release artifacts are signed
differently: container images with keyless cosign, PyPI distributions with
GitHub build-provenance attestations. Both are Sigstore-based and are checked
against Sigstore's online infrastructure by default (§5).

- **future design:** sign the bundle manifest in the release workflow with
  the same keyless Sigstore identity, and ship the Sigstore bundle and trusted
  root alongside it so `cosign` can check it offline. Which key or identity
  signs official bundles is an owner decision; nothing in a release workflow
  builds or signs a bundle today.
- **planned** (tracked in ADR-0249): the container image (multi-arch OCI
  archive), the OCI Helm chart, the docs site and a CVE advisory snapshot as
  bundle members; a release job that publishes the bundle; and
  `nova doctor --check-cves` offline reporting.

The slice-1 form, `make airgap-bundle` (bundles `dist/` as named members, no
dependency wheelhouse), still works, and the same verifier checks it.
