# Release Process

Follow these steps to cut a NovaFabric release.

## 0. Migration compatibility rule (ADR-0180, expand-contract)

Every Alembic migration shipped in a release MUST be backward-compatible with
the previous minor version's code (the N/N+1 window): additive changes first
(new tables/columns nullable or defaulted), destructive contraction (drops,
renames, type narrowing) only after one full minor cycle in which no shipped
code path needs the old shape. A release containing a migration that breaks
the previous minor's code against the new schema is **blocked** until the
migration is split. Rationale and the full posture (single-writer
active-passive, fencing invariant): ADR-0180.

## 0a. API removal rule (ADR-0188, deprecation register)

A `/v0` endpoint listed in the deprecation register (`docs/api-reference.md`,
"Deprecation register") may be removed only in a release that is ≥ its
earliest-removal release and is a **minor** bump pre-1.0 (`0.Y.0`) / **major** bump
post-1.0 (`X.0.0`). The removal lands in the same change that bumps `pyproject.toml`
to that release, and its register row stays, recording the release in the
**Removed in** column. `tests/test_deprecation_removal_gate.py` (part of the unit
suite, so of every gate in step 1) fails the release otherwise.

## 1. Run tests

```bash
make test-par   # the whole suite as CI's `unit` shards run it (--cov=novafabric, --cov-fail-under=90)
```

Required: all tests pass, coverage ≥ 90%.

In CI the same suite runs as six duration-balanced `unit` shards (ADR-0316); a
`coverage` job then enforces the 90% floor over their union and proves that every
test ran in exactly one shard. To reproduce one shard locally:
`make test-shard SHARD=3/6`.

## 1a. Run the NovaSeal latency gate (median gate + NovaSeal p99 tail alarm)

```bash
make benchmark
# or directly:
uv run pytest tests/seal/test_benchmark.py -v --benchmark-json=.benchmark-results/seal_latency.json
```

Required: `NovaSeal.seal()` median < 50 ms over 100 rounds (`seal.seal-call.median` in
[`docs/slo.md`](slo.md)). A p99 ≥ 200 ms (`seal.seal-call.p99`) is a **non-blocking tail
alarm** — a `SealTailLatencyAlarm` warning and a `::warning::` annotation in CI. One alarm
on a shared runner is scheduler noise; the same alarm on consecutive runs is a regression
to investigate before tagging.

## 1b. Verify the remaining CI gates are green

More blocking gates run in CI on every push. A release must not be tagged while any
is red on `main` — and since ADR-0316 it cannot be published either: every
tag-triggered workflow first waits for `ci.yml` on the tagged commit and refuses to
publish unless it concluded `success` (see step 7). Check the Actions tab, or run
locally as below:

- **`lint`** — `uv lock --check`, `ruff check src tests scripts`, and the `nova`
  CLI smoke test (steps 2 and 4); the fast signal, ~1–2 min.
- **`coverage`** — combines the `unit` shards' coverage and enforces the 90% floor
  over the union; fails if any test ran in no shard or in two.
- **`capture-overhead-gate`** — capture-overhead p95 (`make benchmark-capture`; p95
  < 2.0 s over 30 rounds).
- **`golden-capsule`** — builds the wheel, installs it into a clean venv and runs from it
  the EvidenceLab walkthrough (zero-key, then `--profile signed-offline --require-verified`),
  the replay-fidelity oracles in `tests/replay/fidelity`, and one test that mocked replay of
  a default-level capsule never reports success; any skipped or xfailed test fails the job, and
  every `force-include` target must be present in the installed wheel. The wheel is built
  from the checkout, not the one `publish-pypi.yml` uploads. Synthetic fixtures and a
  loopback model only.
- **`web`** — the Astro site + dashboard build and typecheck (`make site`).
- **`integration`** — the testcontainers tier (`uv run pytest tests/integration` with
  Docker available; CI installs with `--all-extras`).

This section exists so the documented release gate names every job in `ci.yml` — guarded by
`tests/docs/test_support_policy.py`, which fails when a job is added there without being
documented here. Note the guard's scope: it reads `ci.yml` only, so a job in a *separate*
workflow file is not covered by it. (The `unit` job's exact command parity is separately guarded
by `tests/docs/test_makefile_matches_ci_gate.py`.)

## 1c. What the tag run will be the first to execute

`publish-pypi.yml`, `publish-image.yml` and `publish-chart.yml` fire only on a `v*` tag (PyPI
wheel + sdist, GHCR image, Helm chart). Nothing on a pull request runs them, so their actions are the one part of the pipeline a green PR says nothing about.

`release-toolchain.yml` covers most of that gap: on any change to the publish path it runs the
same toolchain with publishing disabled — buildx build with `push: false` passing every input the
release step passes, cosign installed and run, chart linted and packaged. Its action versions are
held identical to the publish workflows by `tests/docs/test_release_toolchain_matches_publish.py`,
because a smoke test pinned to different versions is a green check that proves nothing.

Things it still cannot cover, so a tag run is their first execution:

- **`docker/login-action`** — needs real registry credentials, which must never be exposed to a
  pull request. A login failure is at least loud and immediate.
- **`cosign sign` and `actions/attest-build-provenance`** — both need the OIDC identity of a tag
  run. The installer is proven; the signing call is not.
- **arm64** — the smoke build is `linux/amd64`, so emulation is exercised only at release.
- **The copy to GHCR and Docker Hub** (see below) — `release-toolchain.yml` runs the real scan
  gate and the real promotion script against fixtures and two local registries, never against
  GHCR or Docker Hub.

After a release, check those in the run log rather than assuming them.

### The image gate: scan the candidate before any production tag exists (experimental)

*Status: experimental. The ordering below is implemented and covered by tests, but no tag run has
executed it yet; the first tag run after it lands is its first real execution.*

`publish-image.yml` no longer pushes the image to a production registry and scans it afterwards.
Within the one `image` job, in this order:

1. **Build once** (`linux/amd64` + `linux/arm64`, SBOM and provenance) into a throw-away registry
   that lives only for the job, bound to loopback. No GHCR or Docker Hub login exists yet.
2. **Scan that digest on each platform** with the pinned trivy
   (`.github/actions/scan-candidate`). Fixable CRITICAL findings (ADR-0186), a scanner crash, a
   timeout or a missing platform fail the job. trivy scans only the runner's platform unless told
   otherwise, so each platform has its own gate step.
3. **Only then** log in, copy **that digest** to GHCR (and to Docker Hub when `DOCKERHUB_TOKEN` is
   set) with `scripts/release_image.py promote`, and verify the destination bytes. A rebuild never
   happens after the scan.
4. **Sign and attest** the promoted digest. A signature does not follow a copy between registries,
   so it is made on the production digest.
5. **Last, move the release tags** (`X.Y.Z`, `X.Y`, `latest`) with `scripts/release_image.py tag`.
   `X.Y.Z` is immutable: it refuses to point at a different digest.

A failed scan therefore leaves no release tag, signature or attestation in any registry. The copy
in step 3 is addressed by a digest-derived staging tag `scanned-<12 hex>` (a registry needs a tag to
create a manifest); it is not a release tag and is created only for an image that passed the gate.

If the job fails *after* step 3 and before step 5, the scanned digest sits in the registry under its
staging tag, unsigned or signed but untagged, and a re-run is safe. If it fails *during* step 5 some
tags may have moved. Re-running the same workflow rebuilds the image, which gives a different digest,
and the immutable `X.Y.Z` check then refuses it by design; resolve that by tagging the already-scanned
digest by hand (`docker buildx imagetools create -t <repo>:<tag> <repo>@<digest>`) or by cutting the
next patch version.

`publish-chart.yml` is a separate workflow: a failed image gate does not stop the Helm chart for the
same tag from publishing.

## 2. Run ruff

```bash
uv run ruff check src tests scripts
```

Required: zero errors.

## 3. Run mypy

```bash
uv run mypy src
```

Required: zero errors.

## 4. CLI smoke test

```bash
NOVAFABRIC_DB_PATH=/tmp/nf_smoke.db uv run novafabric register tests/fixtures/valid_model.yaml
NOVAFABRIC_DB_PATH=/tmp/nf_smoke.db uv run novafabric list
NOVAFABRIC_DB_PATH=/tmp/nf_smoke.db uv run novafabric inspect fraud-model@1.0.0
NOVAFABRIC_DB_PATH=/tmp/nf_smoke.db uv run novafabric validate tests/fixtures/valid_agent.yaml
NOVAFABRIC_DB_PATH=/tmp/nf_smoke.db uv run novafabric report
NOVAFABRIC_DB_PATH=/tmp/nf_smoke.db uv run novafabric report --format json
rm /tmp/nf_smoke.db
```

Required: all commands exit 0.

## 5. Update CHANGELOG.md

Add a `## [x.y.z] — YYYY-MM-DD` section with Added, Improved, and Fixed entries.

## 6. Bump version

Edit `pyproject.toml`:

```toml
version = "x.y.z"
```

## 7. Commit and tag

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "chore: release vx.y.z"
git tag vx.y.z
git push origin main --tags
```

A tag on a commit whose CI is red, cancelled or missing publishes **nothing**: the
`ci-green` job in `release.yml`, `publish-pypi.yml`, `publish-image.yml` and
`publish-chart.yml` (`.github/workflows/require-green-ci.yml`) waits up to 100 minutes
for CI on that exact commit and fails closed otherwise. Fix CI (or re-run it if it was
cancelled), then re-run the failed tag workflows from the Actions tab — the gate
re-checks and the publish proceeds.

The image job also fails closed on the vulnerability scan: when `publish-image.yml` fails in its
scan step, **no image tag for that version exists** in GHCR or Docker Hub (see §1c, "The image
gate"). Fix the finding, then cut a new tag; a re-run of the failed tag run rebuilds the image and
re-scans it, so it only helps if the finding came from the scanner database or a transient error. The trivy reports for both platforms are attached to the run as
`trivy-report-<sha>`.

GitHub runs push CI only on the head of a push. If the tagged commit was not a head (it was
pushed together with later commits), it has no CI run: start one with
`gh workflow run ci.yml --ref vx.y.z`, then re-run the failed tag workflows.

## 8. GitHub release

There is no manual step: pushing the tag runs `.github/workflows/release.yml`, which
creates the GitHub Release from `docs/releases/<tag>.md`. Write that file **before**
tagging — its first H1 becomes the release title and the rest the body; the tag run
fails if the file is missing.
