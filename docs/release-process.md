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

## 1. Run tests

```bash
uv run pytest --benchmark-disable --cov=novafabric --cov-report=term-missing
```

Required: all tests pass, coverage ≥ 90%.

## 1a. Run the NovaSeal p99 latency gate

```bash
make benchmark
# or directly:
uv run pytest tests/seal/test_benchmark.py -v --benchmark-json=.benchmark-results/seal_latency.json
```

Required: `NovaSeal.seal()` p99 < 200 ms over 100 rounds.

## 1b. Verify the remaining CI gates are green

Three more blocking gates run in CI on every push; a release must not be tagged while
any is red on `main` (check the Actions tab, or run locally as below):

- **`capture-overhead-gate`** — capture-overhead p95 (`make benchmark-capture`; p95
  < 2.0 s over 30 rounds).
- **`web`** — the Astro site + dashboard build and typecheck (`make site`).
- **`integration`** — the testcontainers tier (`uv run pytest tests/integration` with
  Docker available; CI installs with `--all-extras`).

This section exists so the documented release gate names every job in `ci.yml` — guarded by
`tests/docs/test_support_policy.py`, which fails when a job is added there without being
documented here. Note the guard's scope: it reads `ci.yml` only, so a job in a *separate*
workflow file is not covered by it. (The `unit` job's exact command parity is separately guarded
by `tests/docs/test_makefile_matches_ci_gate.py`.)

## 1c. What the tag run will be the first to execute

`publish-image.yml` and `publish-chart.yml` fire only on a `v*` tag. Nothing on a pull request
runs them, so their actions are the one part of the pipeline a green PR says nothing about.

`release-toolchain.yml` covers most of that gap: on any change to the publish path it runs the
same toolchain with publishing disabled — buildx build with `push: false` passing every input the
release step passes, cosign installed and run, chart linted and packaged. Its action versions are
held identical to the publish workflows by `tests/docs/test_release_toolchain_matches_publish.py`,
because a smoke test pinned to different versions is a green check that proves nothing.

Three things it still cannot cover, so a tag run is their first execution:

- **`docker/login-action`** — needs real registry credentials, which must never be exposed to a
  pull request. A login failure is at least loud and immediate.
- **`cosign sign` and `actions/attest-build-provenance`** — both need the OIDC identity of a tag
  run. The installer is proven; the signing call is not.
- **arm64** — the smoke build is `linux/amd64`, so emulation is exercised only at release.

After a release, check those three in the run log rather than assuming them.

## 2. Run ruff

```bash
uv run ruff check src tests
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

## 8. GitHub release

Create a GitHub release from the tag. Copy the relevant CHANGELOG section as the
release body.
