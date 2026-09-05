"""The push gate's cache key must cover every input the suite reads.

`scripts/test-gate.sh` skips the whole suite when its digest matches the stamp in
`.git/nova-test-gate-passed`. That makes the digest a load-bearing correctness
boundary: anything it does not hash is a change that can be pushed with a green
gate the suite never actually ran against.

It used to hash `find src tests scripts -name '*.py'` — Python sources only.
Measured 2026-09-04: after changing `collector/go.mod`, `collector/go.sum` and
`CHANGELOG.md`, the digest was byte-identical to the stamp and the gate printed
*"already GREEN for this exact tree — nothing to run"* about a tree it had never
seen in that state.

The omissions were not exotic. `pyproject.toml` and `uv.lock` are the
highest-risk change class in the repository — a dependency bump could not
invalidate the stamp. The ~186 guards in `tests/docs` assert against `docs/`,
`ROADMAP.md`, `CHANGELOG.md`, `.github/workflows/` and the `Makefile`, none of
which were hashed, so a docs-only edit that broke a docs guard would push green.

Hashing every tracked and untracked-but-not-ignored file costs 0.58 s over 4342
files, so the narrow key was never buying anything worth this.

This guard reads the list straight out of the script (`--print-files`), which is
the same list the digest is computed from, so a future narrowing cannot satisfy
the guard while quietly changing the real cache key.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "test-gate.sh"

# One representative file per input class the gate skips the suite for. Each is
# a real path in this repository and a real thing the suite reads.
MUST_BE_COVERED = [
    ("uv.lock", "dependency lockfile — a bump must never reuse a stamp"),
    ("pyproject.toml", "dependency manifest and tool config"),
    ("Makefile", "guarded against CI by tests/docs/test_makefile_matches_ci_gate.py"),
    (".github/workflows/ci.yml", "read by the release/CI-parity guards"),
    ("collector/go.mod", "Go collector — its own CI job builds from this"),
    ("CHANGELOG.md", "read by the docs guards"),
    ("ROADMAP.md", "read by the docs guards"),
    ("scripts/test-gate.sh", "the gate must hash itself"),
]


def _covered_files() -> set[str]:
    proc = subprocess.run(
        ["bash", str(GATE), "--print-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"gate script not runnable here: {proc.stderr.strip()[:200]}")
    return {line for line in proc.stdout.splitlines() if line}


def test_the_digest_covers_more_than_python_sources() -> None:
    covered = _covered_files()
    assert covered, "--print-files produced nothing; the digest would hash nothing"

    non_python = {p for p in covered if not p.endswith(".py")}
    assert non_python, (
        "the gate's digest covers only .py files — the exact defect this guards "
        "against. A lockfile, workflow or docs change would reuse a stale stamp."
    )


@pytest.mark.parametrize("path,why", MUST_BE_COVERED, ids=[p for p, _ in MUST_BE_COVERED])
def test_high_risk_inputs_are_hashed(path: str, why: str) -> None:
    if not (REPO_ROOT / path).exists():
        pytest.skip(f"{path} is not present in this checkout")

    covered = _covered_files()
    assert path in covered, (
        f"{path} is not part of the push gate's digest, so changing it leaves the "
        f"stamp valid and the suite is skipped for a tree it never ran against.\n"
        f"Why this file matters: {why}\n"
        f"Fix: widen the enumeration in scripts/test-gate.sh (_tree_files)."
    )
