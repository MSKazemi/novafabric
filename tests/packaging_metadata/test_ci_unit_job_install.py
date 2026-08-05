"""The CI `unit` job must install every extra, and shard xdist by group.

Why this guard exists
---------------------
On 2026-08-01 the `unit` job in `.github/workflows/ci.yml` was measured against
its own recipe in a clean venv.  With ``uv sync --frozen`` (no extras) the suite
produced **50 failures and 12 errors** — not skips.  The extras-dependent suites
import their optional dependency at module scope, so an absent extra is a
collection error, never a graceful skip:

===================================  =========================================
missing distribution                 suites that hard-fail without it
===================================  =========================================
``alembic``    (``server`` extra)    ``test_schema_skew``, ``test_db_upgrade_track``
``uvicorn``    (``server`` extra)    ``test_server_local_token_auth``, serve CLI
``nats-py``    (``nats`` extra)      the JetStream ``LineageConsumer`` tier
``a2a-sdk``    (``a2a`` extra)       ``tests/adapters`` A2A interceptor
``mcp``        (``mcp`` extra)       MCP conformance
===================================  =========================================

That breakage was invisible for eleven days: ``tests/coverage/`` shadowed the
``coverage`` distribution, so ``pytest-cov`` could not load and the job went red
before reporting a single test result.  Fixing the shadowing would have exposed
a still-red job, so both had to be fixed together.

The two clauses below are the ones that silently rot.  Neither is a style
preference — dropping either one puts the job back in the red.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def unit_job() -> dict:
    workflow = yaml.safe_load(_CI_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    assert "unit" in jobs, "the CI workflow no longer has a `unit` job"
    return jobs["unit"]


def _run_steps(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_unit_job_installs_all_extras(unit_job: dict) -> None:
    """A bare `uv sync` leaves ~61 tests unable to import their dependency."""
    installs = [r for r in _run_steps(unit_job) if "uv sync" in r]
    assert installs, "the unit job no longer installs the project"
    for cmd in installs:
        assert "--all-extras" in cmd, (
            "the CI unit job must `uv sync --frozen --all-extras`. Without the "
            "extras, alembic/uvicorn/nats-py/a2a-sdk/mcp are absent and their "
            "suites ERROR at import instead of skipping — 61 of them, measured. "
            f"Offending step: {cmd!r}"
        )


def test_unit_job_shards_xdist_by_group(unit_job: dict) -> None:
    """`--dist=loadgroup` is mandatory once the extras are installed.

    ``tests/metadata_store/conftest.py`` pins its whole testcontainers Postgres
    tier into a single ``xdist_group`` so the session shares one container.
    Under the default ``--dist=load`` that group is scattered across workers,
    each racing to start its own container; measured, that alone reproduced 9
    failures in an otherwise-green run.
    """
    pytest_steps = [r for r in _run_steps(unit_job) if "pytest" in r]
    assert pytest_steps, "the unit job no longer runs pytest"
    parallel = [r for r in pytest_steps if "-n auto" in r]
    assert parallel, "the unit job no longer runs pytest in parallel"
    for cmd in parallel:
        assert "--dist=loadgroup" in cmd, (
            "parallel runs must use --dist=loadgroup; plain --dist=load splits "
            f"the metadata_store xdist_group across workers. Offending step: {cmd!r}"
        )


def test_unit_job_still_enforces_the_coverage_floor(unit_job: dict) -> None:
    """The 90% floor is the point of running coverage here at all."""
    pytest_steps = [r for r in _run_steps(unit_job) if "pytest" in r]
    assert any("--cov-fail-under=90" in r for r in pytest_steps), (
        "the unit job must keep enforcing --cov-fail-under=90"
    )


# --------------------------------------------------------------------------
# Lint scope (BL-033)
# --------------------------------------------------------------------------


def test_every_lint_invocation_covers_scripts() -> None:
    """`scripts/` sat outside `ruff check src tests` in all five places the
    scope is defined, so six findings rotted there unnoticed — in code that
    CI actually executes (`pip_audit_gate.py`, `license_gate.py`).

    Pin the scope so it cannot silently narrow again. Checks the declared
    scope of every `ruff check` invocation in the repo's own tooling.
    """
    import re

    repo = Path(__file__).resolve().parents[2]
    sources = [
        repo / "Makefile",
        repo / "CONTRIBUTING.md",
        repo / "CLAUDE.md",
        repo / ".github/workflows/ci.yml",
        repo / ".github/workflows/metadata_store_security_gate.yml",
    ]
    checked = 0
    for path in sources:
        if not path.exists():  # CLAUDE.md is untracked/local-only
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "ruff check" not in line:
                continue
            scope = re.sub(r".*ruff check\s*", "", line)
            assert "scripts" in scope, f"{path.name}: ruff scope omits scripts/ -> {line.strip()}"
            checked += 1
    assert checked >= 4, f"expected to find the ruff invocations, found {checked}"
