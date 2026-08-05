"""The test suite must be runnable by someone who cloned only the public repo.

This repository keeps one working tree and two gits. `design/`, `.claude/`,
`CLAUDE.md`, `monetize/`, `bench/`, `site-config/` and `THREAT_MODEL.md` are
excluded from the public one, and `.gitignore` additionally drops some file
types wholesale.

All of it is present on a maintainer's disk. So a test that reads a private file
passes locally and fails for **every outside contributor**, and for CI — which is
exactly what happened: five modules read their fixture corpora from
`design/spec/fixtures/`, and the `unit` job failed on them for as long as it was
able to report anything at all.

The rule this enforces is the same one `scripts/check_doc_links.py` applies to
documentation: **the question is whether the public git tracks it, not whether
the file exists.** Those two answers differ precisely where it matters.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Trees the public git never contains.
_PRIVATE_ROOTS = (
    "design",
    ".claude",
    "monetize",
    "bench",
    "site-config",
)


# Modules that name a private root but do not *require* it: they scan it when it
# happens to be present and are correct without it. Each needs a reason.
_ALLOWED = {
    # Scans `design/` for phantom extras references when the tree exists, and
    # excludes `design/adr/` from that scan. A public clone simply scans less.
    "tests/docs/test_extras_references.py",
}


def _tracked() -> frozenset[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return frozenset(out.splitlines())


def _test_sources() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "tests/*.py", "tests/**/*.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [_REPO_ROOT / rel for rel in out.split()]


def test_no_test_module_reads_from_a_private_tree() -> None:
    """A path built from a private root is unreadable for every outside clone."""
    offenders: list[str] = []

    for path in _test_sources():
        if path.name == Path(__file__).name or not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - caught by the suite itself
            continue

        rel = str(path.relative_to(_REPO_ROOT))
        if rel in _ALLOWED:
            continue

        # Only flag a private name used to BUILD A PATH — `_ROOT / "design"` —
        # not any string that happens to equal one. `tenant="bench"` is a tenant
        # identifier, and treating it as a path reference is a false positive
        # that would train people to ignore this test.
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            for side in (node.left, node.right):
                if (
                    isinstance(side, ast.Constant)
                    and isinstance(side.value, str)
                    and side.value in _PRIVATE_ROOTS
                ):
                    offenders.append(f"{rel}:{side.lineno} builds a path into {side.value!r}")

    assert not offenders, (
        "these tests build paths into a tree the public git does not contain, so "
        "they pass for a maintainer and fail for every outside contributor:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n\nMove the fixture into tests/fixtures/ and commit it."
    )


@pytest.mark.parametrize(
    "fixture_dir",
    [
        "tests/fixtures/spec/pii-masking-pipeline",
        "tests/fixtures/spec/dataset-experiment",
        "tests/fixtures/spec/batch-blob-export",
        "tests/fixtures/spec/saved-views",
    ],
)
def test_relocated_fixture_corpora_are_tracked(fixture_dir: str) -> None:
    """The corpora moved out of `design/` on 2026-08-05 must stay public.

    Copying them was only half the fix — an untracked copy fails exactly the same
    way, and silently.
    """
    tracked = _tracked()
    files = [p for p in tracked if p.startswith(f"{fixture_dir}/")]

    assert files, f"{fixture_dir} is not tracked by the public git"


# NOTE: the `*.pem` gap is deliberately NOT asserted here.
#
# `.gitignore` drops `*.pem` wholesale — a correct default for real keys, and it
# also drops the six deliberately-public fixture keys under
# `tests/fixtures/promote/keys/`, so every `promote` test fails on a public
# clone. Closing it means either committing private-key material to a public
# repository or generating the keys at test time. For a project whose premise is
# verifiable provenance that is a security-shaped decision, not housekeeping, so
# it is filed rather than patched: see issue #24.
#
# A test that passed while describing a known failure would be worse than no
# test at all.
