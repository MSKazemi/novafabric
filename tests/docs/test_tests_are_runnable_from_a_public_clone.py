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
import re
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


def test_no_private_key_material_is_committed() -> None:
    """The blanket `*.pem` rule must keep holding.

    Issue #24 was closed by *generating* the promote fixture keys at session
    start rather than committing them, so this asserts the outcome that made
    that the right call: no key material in the public repository, and the suite
    still runnable without it.
    """
    tracked = _tracked()
    pems = sorted(p for p in tracked if p.endswith(".pem"))

    assert not pems, (
        "private-key material is committed to the public repository: "
        f"{pems}. Test keys are minted at session start by "
        "tests/conftest.py::_materialise_promote_keys, which runs the existing\n        tests/fixtures/promote/generate_keys.py — nothing needs committing."
    )


def test_the_promote_key_factory_is_public() -> None:
    """The *generator* must be tracked, even though its output must not be.

    Committing the factory and not the keys is the whole trick; an untracked
    factory would put a public clone back where it started.
    """
    assert "tests/fixtures/promote/generate_keys.py" in _tracked()


def test_no_workflow_command_depends_on_a_private_path() -> None:
    """CI must not run a script the public repository does not contain.

    Found on 2026-08-06: the MetadataStore Security Gate ended with
    `bash bench/rls_partition_pruning/ci_smoke.sh`, and `bench/` is excluded from
    the public git — so that step could never pass. It stayed invisible because
    the step *above* it was also failing, which skipped everything after.

    Same rule as the tests and the docs, one layer up: the question is whether
    the public git tracks the path, not whether it exists on somebody's disk.
    Comments are excluded — several workflows legitimately cite a `design/`
    document as provenance without reading it.
    """
    tracked = _tracked()
    workflows = sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    offenders: list[str] = []

    pattern = re.compile(
        r"(?<![\w/.-])(" + "|".join(_PRIVATE_ROOTS) + r")/[\w./-]+"
    )

    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            for match in pattern.finditer(code):
                path = match.group(0)
                if path in tracked:
                    continue
                # An existence-guarded reference is fine: the step degrades
                # instead of failing, which is how a workflow serves both the
                # public repo and the private mirror. The guard is looked for
                # anywhere in the same workflow, because `[ -f x ]` and the
                # command it protects are on different lines.
                if f"-f {path}" in text or f'-f "{path}"' in text:
                    continue
                offenders.append(f"{workflow.name}:{lineno} runs against {path!r}")

    assert not offenders, (
        "these CI steps reference paths the public git does not contain, so they "
        "cannot succeed on a public checkout:\n  " + "\n  ".join(offenders)
    )
