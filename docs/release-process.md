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
