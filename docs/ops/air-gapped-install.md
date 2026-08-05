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
(`ghcr.io/novafabric/novafabric`, `oci://ghcr.io/novafabric/charts/novafabric`
— [Server Deployment Guide](server-deployment.md)); mirror them into your
private registry (`docker pull` → `docker push registry.internal/...`) —
nothing in the chart requires the public registry at runtime.

---

## 3. Offline identity: ed25519 tokens, no IdP

**Status: experimental**
([ADR-0018](../decisions.md);
`src/novafabric/server/offline_tokens.py`). This is the intended auth mode
for air-gapped clusters, SLURM batch jobs, and CI without an OIDC provider.

- `nova server` generates (or you point it at) an **ed25519 keypair**
  (`offline_key_path` in `nova-server.yaml`, or
  `NOVAFABRIC_OFFLINE_KEY_PATH`). JWTs are signed **and verified locally** —
  no external endpoint is ever contacted.
- Issue: `nova server issue-token --subject worker-01 --roles reader,writer
  --expires-in 30d` (prints the JWT).
- Revoke: `nova server revoke-token <jti>`. Issue/revoke records live in a
  `token_audit` SQLite DB stored **adjacent to the key**
  (`<key-name>-tokens.db`); revoked tokens get 401 on the next call.
- Verification checks the ed25519 signature, the `exp` claim, and the
  revocation record — all local I/O.
- The key file must be mode 0600 and owned by the server user;
  `nova server start` refuses a world-readable key.

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
| **RFC 3161 timestamps** (NovaSeal `tsa_url`) | HTTPS POST to the timestamp authority (default example config uses the public `freetsa.org`) | **Omit `tsa_url`** (or set it `""`): NovaSeal still signs with ECDSA, just without a timestamp token — `nova verify` then reports the timestamp check accordingly, which is expected, not tampering. Or run a **private TSA inside the enclave** and point `tsa_url` at it ([operator guide §5b](../operator-guide.md), [NovaSeal configuration](../novaseal-configuration.md)). The timestamp-verification code additionally has an explicit HPC air-gap mode (`offline_mode=True` skips nonce-store writes and all network calls — `src/novafabric/trust/novaseal/timestamp.py`) |
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
# /etc/novafabric/nova-server.yaml
backend: postgres            # in-network Postgres; or sqlite for single-host
server:
  host: "127.0.0.1"          # or your internal interface
  port: 7433
offline_key_path: "/etc/novafabric/keys/offline-key.pem"
# oidc.enabled defaults to false
otel_endpoint: ""            # disabled
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
export NOVA_DSN="postgresql://nova:***@pg.internal:5432/novafabric"
nova server start --backend postgres
nova server issue-token --subject batch-runner --roles writer --expires-in 90d
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
