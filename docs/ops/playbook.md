# Operator playbook: install, verify, back up, upgrade, recover

> **Written for NovaFabric v0.109.0** (tag `v0.109.0`, commit `9b7b31b9`, released 2026-11-22).
> On 2026-11-27 the v0.109.0 wheel was downloaded from PyPI, checked as in §2, installed with no extras
> into a clean Python 3.12 environment, and the local-mode checklists in §4 to §6 were run end to end
> against it in a throwaway home. Every `nova` command and flag on this page resolves in that build.
> Commands that were not run say so where they appear.
> Only the latest release is supported before v1.0 ([support policy](../support-policy.md)). If you
> run a different version, read the release notes in [`docs/releases/`](../releases/) for every
> version between yours and the one on this page before you follow it.

This page puts the operator's job in order, as numbered checklists. Install, check the install, check
the release artifacts, back up, restore, upgrade, roll back, and recover from the most common failures.
It links to the detailed pages and does not copy them. When this page and a detailed page disagree,
the detailed page wins, and the disagreement is a bug in this page.

**Maturity labels** follow the [docs honesty rule](../../CONTRIBUTING.md#documentation-status-labels):
**works today** · **experimental** (implemented, interfaces may change) · **planned** (on the
[roadmap](../../ROADMAP.md) with a target) · **future design** (intent only). A label applies to a
NovaFabric feature. Third-party tools (`pip`, `cosign`, `gh`, `helm`, `pg_dump`) carry no NovaFabric
label; instead, each step says whether it was run for this page.

## 0. Find the version you have

| Where it runs | Command | What you see on v0.109.0 |
|---|---|---|
| CLI (pip, uv, pipx) | `nova --version` | `novafabric 0.109.0` |
| Python environment | `python -m pip show novafabric` | `Version: 0.109.0` |
| Container image | `docker run --rm --entrypoint nova ghcr.io/mskazemi/novafabric:0.109.0 --version` | `novafabric 0.109.0` (the image's own entrypoint waits for Postgres, so override it) |
| Container image (no run) | `docker image inspect <image> --format '{{index .Config.Labels "org.opencontainers.image.version"}}'` | `0.109.0` |
| Helm release | `helm list` | `CHART` is `novafabric-<version>`. The chart's `appVersion` is the image tag it deploys unless `image.tag` is set |
| Running server | `GET /v0/version` with a `reader` token ([monitoring](monitoring.md) §3) | structured build identity |

The publish workflows derive the chart version, the chart's `appVersion` and the image tag from the
git tag (`publish-chart.yml` packages with `--version` and `--app-version` taken from the tag). The
PyPI version comes from `pyproject.toml`, which the [release process](../release-process.md) sets to
the same number before tagging. So one number names all four for a given release.

---

## 1. Pick your path

| Target | Install guide | Maturity |
|---|---|---|
| Laptop, CI job, researcher (CLI only) | [Installation](../installation.md) "Light" + [Getting Started](../getting-started.md) Step 1 | **works today** |
| Local dashboard | [Installation](../installation.md) "Desktop" + [Dashboard](../dashboard.md) | core **works today**, dashboard **experimental** |
| Shared server on one host (Postgres) | [Server deployment](server-deployment.md) Scenario 2, then Scenarios 3/4 for auth | **experimental** |
| Container / Compose | [Server deployment](server-deployment.md) Scenario 6. Compose secret handling changes in PR #345 (in review) | **experimental** |
| Kubernetes (Helm) | [Helm chart README](../../deploy/helm/novafabric/README.md) + [Server deployment](server-deployment.md) Scenario 5 | **experimental** |
| Single host under systemd (Ansible) | No Ansible role in v0.109.0. One is proposed in PR #354 (in review) | not in v0.109.0 |
| Air-gapped site | [Air-gapped install](air-gapped-install.md) §2 (wheel mirroring). A complete wheelhouse bundle and offline verifier are proposed in PR #353 (in review) | wheel mirroring **works today** |
| HPC / SLURM / cluster capture | [Operator guide](../operator-guide.md) §3 + [Cluster-scale migration](cluster-scale-migration.md) | **experimental**, partial |

For every path side by side (OS, services, storage, auth, TLS, backup, upgrade, and what CI actually
runs for each one), see the [deployment matrix](deployment-matrix.md).

**Tested versions** ([support policy](../support-policy.md), **works today**):

- Python **3.12**. `requires-python` is `>=3.12`, and every job in `ci.yml` runs 3.12 only.
- PostgreSQL **16**, the service image the CI integration tier runs.
- Linux x86_64. The image is also built for arm64 but is not tested there.
- No Kubernetes version is tested. CI lints and packages the chart (`release-toolchain.yml`,
  `publish-chart.yml`) but never installs it into a cluster.

---

## 2. Check the release artifacts before you install them

What v0.109.0 publishes, and what each artifact carries:

| Artifact | Where | Integrity evidence | Check |
|---|---|---|---|
| Wheel + sdist | PyPI `novafabric` | SHA-256 digests on PyPI; GitHub artifact attestation (SLSA build provenance) signed by `publish-pypi.yml`; uploaded by Trusted Publishing (OIDC, no stored token) | 2a: **works today** (run for this page) |
| Wheel SBOM (CycloneDX) | artifact of the `publish-pypi.yml` run, not a release asset | none beyond the run itself. The step is `continue-on-error`, so a release can ship without one | download it from the Actions run |
| Container image (amd64, arm64) | `ghcr.io/mskazemi/novafabric:<X.Y.Z>` | keyless cosign signature (Fulcio + Rekor) on the digest; GitHub attestation (SLSA provenance); BuildKit SBOM + provenance; trivy report as a run artifact; the workflow fails on a fixable CRITICAL finding, but only after the image is pushed and signed | 2b: **works today** (run for this page) |
| Helm chart | `oci://ghcr.io/mskazemi/charts/novafabric` | **no signature and no attestation.** The OCI digest is the only integrity handle | 2c: digest pinning only |
| GitHub Release | `v<X.Y.Z>` on the repository | release notes only. **No files are attached** to the v0.109.0 Release | n/a |

Values for **v0.109.0**, read from PyPI and GHCR on 2026-11-27:

| Item | Expected value |
|---|---|
| `novafabric-0.109.0-py3-none-any.whl` SHA-256 | `f4a38fd3e992478c029259bee49037ee4dd72111923332bc4ee002158cb17eb2` |
| `novafabric-0.109.0.tar.gz` SHA-256 | `d1bb9c0cebce50ef8ee98f7d17e926cc63eee49cfd36d4aeb348c163a9af4117` |
| Image digest for tag `0.109.0` | `sha256:f6e4a0d2852015aa3c422539719e733c35085712c3dff5ab60604a2cb5c69760` |
| Chart `0.109.0` OCI manifest digest | `sha256:9b4a7e60771d573ec59d7a8b8ec7eec72ff0c0e2990b765ea1b815d500a6315f` |
| Signer of the wheel attestation | `https://github.com/MSKazemi/novafabric/.github/workflows/publish-pypi.yml@refs/tags/v0.109.0` |
| Signer of the image signature and attestation | `https://github.com/MSKazemi/novafabric/.github/workflows/publish-image.yml@refs/tags/v0.109.0` |
| OIDC issuer (both) | `https://token.actions.githubusercontent.com` |

For another version, derive the same values from the same sources (the commands below print them).
Do not copy them from this page. A signed, machine-readable release manifest that carries these values
is not published today. It is tracked in issue #221 (open).

### 2a. Python package

1. Download the exact version without installing it:

   ```bash
   V=0.109.0
   python -m pip download "novafabric==$V" --no-deps --only-binary=:all: -d nf-dl/
   ```

2. Compare its SHA-256 with the digest PyPI publishes:

   ```bash
   python -m pip hash nf-dl/novafabric-$V-py3-none-any.whl
   curl -s "https://pypi.org/pypi/novafabric/$V/json" | python3 -c \
     'import json,sys; [print(u["filename"], u["digests"]["sha256"], "YANKED" if u["yanked"] else "") for u in json.load(sys.stdin)["urls"]]'
   ```

   The two digests must match, and no file may say `YANKED`.

3. Check the build provenance, pinned to the publishing workflow and the tag:

   ```bash
   gh attestation verify nf-dl/novafabric-$V-py3-none-any.whl \
     --repo MSKazemi/novafabric \
     --signer-workflow MSKazemi/novafabric/.github/workflows/publish-pypi.yml \
     --source-ref "refs/tags/v$V"
   ```

   Exit status 0 means the attestation verified. Add `--format json` to see the certificate. The same
   attestation also covers the sdist. With `--signer-workflow` set to another workflow, the check
   fails (exit 1), so the pin is doing work.

4. Install the file you checked, not a fresh download: `python -m pip install nf-dl/novafabric-$V-py3-none-any.whl`.
   This checks only the NovaFabric distribution. To hash-pin its dependencies as well, use a fully
   pinned requirements file with `--require-hashes`, or the offline wheel mirror in
   [Air-gapped install](air-gapped-install.md) §2.

PyPI also serves a PEP 740 provenance record for the v0.109.0 wheel, written by the PyPI upload action.
This project documents no check for it. Step 3 is the documented one.

### 2b. Container image

Use a cosign 3.x client. The release signs with cosign v3.0.6 (pinned in `publish-image.yml`), and the
check below was run with that version.

1. Check the signature with an exact identity, not a prefix match:

   ```bash
   V=0.109.0
   cosign verify "ghcr.io/mskazemi/novafabric:$V" \
     --certificate-identity "https://github.com/MSKazemi/novafabric/.github/workflows/publish-image.yml@refs/tags/v$V" \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```

   cosign prints the claims it checked and a JSON payload whose `docker-manifest-digest` is the image
   digest. [Server deployment](server-deployment.md) Scenario 5 shows a looser variant that accepts
   any workflow in the repository.

2. Check the build provenance:

   ```bash
   gh attestation verify "oci://ghcr.io/mskazemi/novafabric:$V" \
     --repo MSKazemi/novafabric \
     --signer-workflow MSKazemi/novafabric/.github/workflows/publish-image.yml \
     --source-ref "refs/tags/v$V"
   ```

3. Deploy by digest, not by tag. Tags such as `0.109.0`, `0.109` and `latest` can be moved; a digest
   cannot. Example: `ghcr.io/mskazemi/novafabric@sha256:f6e4a0d2852015aa3c422539719e733c35085712c3dff5ab60604a2cb5c69760`.

Docker Hub is an optional mirror that is pushed only when the repository has a Docker Hub token
configured. The signature and attestation above are made for the GHCR reference.

### 2c. Helm chart

The chart is not signed and has no attestation, so pinning its digest is the only check available.
(The Helm commands below follow the chart README and Helm's CLI. They were not run for this page: the
digest in the table above was read from the GHCR registry API.)

1. `helm pull oci://ghcr.io/mskazemi/charts/novafabric --version 0.109.0` prints a `Digest:` line.
   Compare it with the expected chart digest.
2. `helm show chart novafabric-0.109.0.tgz`. `version` and `appVersion` must both be `0.109.0`.
3. Install from the file you checked (`helm install nova ./novafabric-0.109.0.tgz …`) and set
   `image.repository` + `image.tag`, or your registry's digest form, to the image you verified in 2b.

### 2d. If a release is broken

**Not documented as a procedure in v0.109.0.** The repository has no yank, deprecation or revocation
runbook, and nothing reconciles a partial publication automatically. Issue #221 (open) tracks this.
What does exist today:

- **works today:** a tag whose CI is not green on that exact commit publishes nothing to any channel
  (the `ci-green` job in each publish workflow; [release process](../release-process.md) §7).
- PyPI marks a yanked file. Step 2a.2 prints `YANKED` for it.
- The fix for a broken release is a new release. Only the latest tag is supported, and earlier tags
  get no fixes ([support policy](../support-policy.md)).

---

## 3. Install

1. Choose the path in §1 and follow its install guide.
2. Pin the version on every channel:
   - pip: `python -m pip install "novafabric==0.109.0"`. Add only the extras you use, for example
     `'novafabric[server]==0.109.0'`; [Installation](../installation.md) lists them.
   - image: the digest you verified in §2b.
   - chart: `--version 0.109.0`, or the `.tgz` you verified in §2c.
3. Server, container and Helm paths: provision Postgres, TLS, auth and secrets yourself. Neither the
   package nor the chart does this for production. The chart's bundled Postgres is for evaluation only
   ([Helm README](../../deploy/helm/novafabric/README.md)).
4. Do not expose `nova serve` (the dashboard) beyond loopback as a substitute for a server. It is not
   read-only ([Installation](../installation.md), "Desktop").

---

## 4. Check the install

**Status: works today** (the seal steps are **experimental**: NovaSeal is opt-in).

Run the smoke test in a throwaway home, so it touches neither your real capsules nor your real audit
log. `NOVAFABRIC_AUDIT_LOG_PATH` matters: the audit log location does not follow `NOVAFABRIC_HOME`.

1. Check the CLI and the environment:

   ```bash
   nova --version                 # novafabric 0.109.0
   nova doctor                    # exit 0
   nova doctor --check-extras     # lists each extra as complete or incomplete, with the install command
   ```

2. Capture, check and verify one run in an isolated home:

   ```bash
   T=$(mktemp -d)
   (
     set -e
     unset NOVAFABRIC_CAPSULE_DIR
     export HOME="$T" XDG_CONFIG_HOME="$T/.config" XDG_DATA_HOME="$T/.local/share" \
            NOVAFABRIC_HOME="$T/nf" NOVAFABRIC_AUDIT_LOG_PATH="$T/audit.jsonl"
     nova seal init                                        # local signing key + local CA, offline
     nova capture python3 -c "print('hello from nova')"    # exits with the workload's exit code
     RUN="$NOVAFABRIC_HOME/capsules/$(ls "$NOVAFABRIC_HOME/capsules" | tail -1)"
     nova validate "$RUN"                                   # ✓ Valid capsule: <run-id>  status=success
     nova verify "$RUN"                                     # signature_ok=True … identity_trust=self-asserted
     nova verify --ca-bundle "$NOVAFABRIC_HOME/keys/novaseal/ca.crt.pem" "$RUN"   # … ca_chain_ok=True, identity_trust=local-ca-pinned
     nova seal log verify                                   # Consistency: OK
   )
   ```

   On v0.109.0 every command exits 0. `timestamp_ok=None` is expected: no timestamp authority is
   configured, so the seal carries no trusted time ([Getting Started](../getting-started.md), "Seal your
   captures"). If `python3` is not on `PATH`, the capture still writes a capsule, records
   `status=failure` and exit code 127, and exits 127.

3. Check storage: `nova doctor --check-storage`. A new home reports `Schema version  not initialised`
   and `Migrations  pending`, and suggests `nova db upgrade --track registry --backend sqlite`.
   - Local mode does not need that step: capture, validate, verify, backup and restore all ran on a
     home in this state.
   - `nova db upgrade` needs the `server` extra (it imports Alembic and SQLAlchemy). On a base install
     it fails with `Error: alembic upgrade failed (ModuleNotFoundError)`. With
     `'novafabric[server]==0.109.0'` installed it succeeds, and the report then reads
     `Schema version  1` and `Migrations  up to date`.

4. Server, container and Helm paths: also probe `/livez` and `/readyz`, and call `/v0/version` with a
   token ([monitoring](monitoring.md); [Helm README](../../deploy/helm/novafabric/README.md) shows how
   to read the pod's local token).

5. Remove the throwaway home: `rm -rf "$T"`.

---

## 5. Back up

The authoritative procedure is the [Backup & restore runbook](backup-restore.md). Back up before
every upgrade.

**Local mode. `nova backup create` / `nova backup verify` are experimental, and the run below was done
for this page.**

1. Create a backup set (registry snapshot via SQLite's online-backup API, capsules, state stores,
   redacted config, a manifest that is DSSE-signed when a local NovaSeal profile exists):

   ```bash
   nova backup create -o novafabric-$(date +%F).tar.gz
   ```

   The output lists a coverage row for every store. `keys: excluded` is the default. Signing keys are
   backed up separately ([NovaSeal key management](../novaseal-key-management.md)).
2. Check it offline: `nova backup verify novafabric-<date>.tar.gz`. Expect `Members: N ok / N total`
   and `✓ Backup set verified`. Exit 1 on any mismatch.
3. Store the set encrypted, with restricted access. It contains `dek.db`, the per-subject data keys
   (runbook, "Custody note").

**Server mode (Postgres).** `pg_dump` **works today**. The verified set
(`nova backup create --profile pg -o <set>.tar.gz`, with the DSN in `NOVA_DSN`, never on the command
line) is **experimental** and was not run for this page. See runbook §1.2. Point-in-time recovery is
Postgres's job (WAL archiving or your provider's PITR).

---

## 6. Restore

**Local mode: `nova restore` is experimental, and the run below was done for this page. Postgres
restore: experimental, not run for this page.**

1. Restore into an empty home. A non-empty home is refused without `--force`, which moves the old data
   aside instead of deleting it:

   ```bash
   nova restore novafabric-<date>.tar.gz --home /path/to/empty-home
   ```

   It verifies the set first (you cannot skip this), extracts, migrates to head, replays crypto-shreds,
   and checks storage and the seal log. It prints `✓ Restore complete — verification chain passed` only
   when every step passes, and exits 1 otherwise.
2. Postgres: `nova restore <set>.tar.gz --dsn "$TARGET_DSN"` runs the whole sequence, including the
   migration to head. After the manual `pg_restore` fallback instead, run
   `nova db upgrade --track registry --backend postgres` yourself. A bare `nova db upgrade` migrates a
   different database (the MetadataStore tier). See runbook §1.2.
3. **A restore is not done until these pass** (runbook §1.5):

   ```bash
   nova doctor --check-storage
   nova seal log verify
   nova validate <restored-capsule-dir>
   nova verify <restored-capsule-dir>        # needs no private key; add --ca-bundle to pin the CA
   nova ledger verify <restored-capsule-dir> # only for capsules anchored with `nova ledger anchor`
   ```

   `nova ledger verify` exits **10** (`NO_CHECKPOINT`) on a capsule that was never anchored. That
   means there is no ledger to check, not that the restore failed.
4. **Known defect in v0.109.0, found while writing this page.** The backup set does not record empty
   directories. A plain `nova capture` leaves an empty `inputs/` in the capsule, so after a round trip
   `nova validate` fails on that capsule with `✗ missing directory: inputs` (exit 1), although
   `nova restore` reported success and `nova verify` still passes. Workaround, after
   `nova backup verify` has passed on the set: recreate only the empty directory
   (`mkdir "<restored-capsule-dir>/inputs"`). `nova validate` and `nova verify` then both exit 0. Do not
   copy or edit any file inside the capsule.
5. Rehearse it. A scripted single-host restore drill (runbook §1.8) is proposed in PR #341 (in review).

Moving capsules between instances, rather than restoring one: `nova export-blob` + `nova import`,
**experimental** (runbook §1.7).

---

## 7. Upgrade

The authoritative procedure is the [Upgrade guide](upgrade-guide.md). The contract is N → N+1 for one
minor at a time (**works today** as a release gate). Skipping minors is outside it.

1. Read the release notes for each version you will cross ([`docs/releases/`](../releases/), section
   "Behaviour changes and upgrade notes"). v0.109.0 changes strict replay, what a capture stores, and
   some audit/compliance exit codes ([v0.109.0 notes](../releases/v0.109.0.md)).
2. Back up and verify the set (§5).
3. Stop every writer: `nova serve`, `nova server`, running captures. Never run two writers against one
   database.
4. Install the new version, pinned and checked (§2, §3): `python -m pip install -U "novafabric==<new>"`.
5. Migrate the registry track: `nova db upgrade --track registry --backend postgres` (or
   `--backend sqlite`). Upgrade guide §4 explains why `--track registry` matters. The command needs the
   `server` extra (§4 step 3).
6. Start the writer again: `nova server start --backend postgres` (server mode) or
   `nova serve --experimental` (dashboard).
7. Re-run §4 against the real home, and `nova validate` a recent capsule.

Helm: `helm upgrade nova oci://ghcr.io/mskazemi/charts/novafabric --version <new>`, keeping
`replicaCount: 1`. The init container migrates on rollout. A release older than v0.102.0 refuses to
upgrade until you set `upgradeAcknowledged: true` ([Server deployment](server-deployment.md)
Scenario 5). Not run for this page.

> **Known defect (container and Helm server mode).** The image entrypoint and the Helm init container
> run `nova db upgrade --backend postgres`, which migrates the MetadataStore track against the same
> database the server's schema guard reads. `nova server start` can then refuse with `E-SKEW-AHEAD`,
> and its "upgrade the package" advice does not apply. Read [Upgrade guide](upgrade-guide.md) §4a
> before you upgrade a container or Helm server deployment.

---

## 8. Roll back

Supported ([Upgrade guide](upgrade-guide.md) §7):

1. **Code back by one minor, schema left as it is.** `python -m pip install "novafabric==<previous>"`,
   or `helm history nova` then `helm rollback nova <REVISION>` (Helm commands not run for this page).
   - The server's schema guard refuses older code on a schema it does not know (`E-SKEW-AHEAD`,
     `src/novafabric/server/schema_skew.py`). So if the release you are leaving added a registry
     migration, the older `nova server start` refuses to start. `NOVAFABRIC_ALLOW_SCHEMA_SKEW=1` is the
     documented break-glass. It is unsupported for normal operation; unset it after the incident.
   - **v0.109.0 → v0.108.0:** no migration file changed between the two tags (every Alembic tree under
     `alembic/`, `src/novafabric/metadata_store/migrations/` and `src/novafabric/migrations/` is
     identical), so this rollback involves no schema change.
2. **Anything further, or a failed migration:** restore the pre-upgrade backup (§6) and reinstall the
   version that wrote it.

Not supported: schema downgrades (`alembic downgrade`), Postgres → SQLite, and downgrading capsules or
sealed evidence.

---

## 9. Recover from the common failures

First moves for any incident ([Incident runbook](incident-runbook.md)):
`nova doctor --check-storage`, then `nova support-bundle` (a secret-safe diagnostics tarball,
**experimental**).

| Symptom | First check | Where to go |
|---|---|---|
| `nova: command not found`, or the wrong version | `python -m pip show novafabric`; `which -a nova` | §0, §3 |
| A command says an extra is missing | `nova doctor --check-extras` (prints the install command) | [Installation](../installation.md) |
| `nova doctor --check-storage` says `Migrations pending` | `nova db upgrade --track registry --backend <sqlite\|postgres>` (needs the `server` extra; a new local home can stay as it is, §4 step 3) | [Upgrade guide](upgrade-guide.md) §4 |
| Server refuses to start with `E-SKEW-BEHIND` | schema behind the code: same command as above | [Upgrade guide](upgrade-guide.md) §4a |
| Server refuses with `E-SKEW-AHEAD` | old code on a newer schema (§8), or the container/Helm defect (§7) | [Upgrade guide](upgrade-guide.md) §4a |
| `/readyz` fails, or the pod restart-loops | read the `checks` object in the `/readyz` body | [Incident runbook](incident-runbook.md) §1 |
| Every request returns 401 | the local token is in `$NOVAFABRIC_HOME/.server-token`; under systemd/containers it is not printed | [Incident runbook](incident-runbook.md) §2 |
| `nova verify` fails a check | read the itemised ✓/✗ lines. Treat a signature failure as an integrity incident | [Incident runbook](incident-runbook.md) §6 |
| Runs on disk but missing from the dashboard | restart `nova serve` (rebuilds its runs cache) | [Incident runbook](incident-runbook.md) §5 |
| Disk full under the home | free space with retention, not by hand-deleting capsules | [Incident runbook](incident-runbook.md) §4 |
| Restored capsule fails `nova validate` with `missing directory: inputs` | the known defect in §6 step 4 | §6 |
| A restore reports a failed step | it exits 1 and its per-step receipts name the step | [Backup & restore runbook](backup-restore.md) |

---

## What this page does not cover

- **Release engineering** (cutting a tag, the CI-green gate): [release process](../release-process.md).
- **A signed release manifest, post-publish verification from a clean runner, and yank/deprecate
  procedures:** issue #221 (open). Not available in v0.109.0.
- **Ansible single-host install:** PR #354 (in review). **Complete air-gap wheelhouse with an offline
  verifier:** PR #353 (in review). **Compose secrets bootstrap:** PR #345 (in review).
  **Deployment matrix with per-path CI evidence:** [deployment-matrix.md](deployment-matrix.md). **CI workflow matrix:** PR #360
  (in review). Until they merge, this page does not depend on any of them.
