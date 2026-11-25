"""The push gate runs the smallest tier that can still see the change — and fails SAFE.

`scripts/test-gate.sh` used to run the whole fast suite (~30 min on a loaded laptop)
for every push, including a one-line Markdown edit. It now asks
`scripts/testsel.py --mode gate` for a tier: none, docs, scoped or full. That makes
the selector a load-bearing correctness boundary, so these guards are mostly about
the escalation paths:

* a path class the gate cannot reason about runs the full suite;
* every non-full tier still runs all of tests/docs;
* a push to main never accepts the import-closure guess (`scoped`), a tag push or a
  push whose refs are unknown always runs everything;
* the stamp is writable inside a git worktree (it used to be a hard-coded
  `.git/...` path, and `.git` is a file there);
* the pre-push hook derives the requirement from git's ref list, and assumes the
  worst when it gets none.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SELECTOR = REPO / "scripts" / "testsel.py"
GATE = REPO / "scripts" / "test-gate.sh"
HOOK = REPO / "scripts" / "hooks" / "pre-push-test-gate.sh"


@pytest.fixture(scope="module")
def testsel():
    spec = importlib.util.spec_from_file_location("_testsel_gate", SELECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A small synthetic index and source set, so each rule is tested on its own.
INDEX = {
    "tests/docs/test_a.py": [],
    "tests/docs/test_b.py": [],
    "tests/docs/conftest.py": [],
    "tests/cli/test_reads_tour.py": [],
    "tests/cli/test_reads_docs_dir.py": [],
    "tests/cli/test_unrelated.py": ["novafabric.cli"],
    "tests/integration/test_names_tour.py": [],
    "tests/core/test_core.py": ["novafabric.core"],
}
TEXTS = {
    "tests/docs/test_a.py": "",
    "tests/docs/test_b.py": "",
    "tests/docs/conftest.py": "",
    "tests/cli/test_reads_tour.py": 'p = "docs/tutorials/feature-tour.md"',
    "tests/cli/test_reads_docs_dir.py": 'root = REPO / "docs"',
    "tests/cli/test_unrelated.py": "documentation docstring dockets",
    "tests/integration/test_names_tour.py": "feature-tour.md",
    "tests/core/test_core.py": "",
}
# Filler, so the rules above select well under the 40% cap.
for _i in range(30):
    INDEX[f"tests/filler/test_{_i}.py"] = ["novafabric.other"]
    TEXTS[f"tests/filler/test_{_i}.py"] = ""


@pytest.fixture
def selected(testsel):
    def run(paths: list[str], index: dict | None = None, texts: dict | None = None):
        # Every synthetic test file "exists", so the runnable filter keeps it.
        return testsel.select_gate(
            paths, index or INDEX, texts or TEXTS, is_file=lambda _rel: True
        )

    return run


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "tests/conftest.py",
        "scripts/test-gate.sh",
        "scripts/testsel.py",
        ".github/workflows/ci.yml",
        "schemas/run-capsule.schema.json",
        ".dualgit/public.allow",
        "src/novafabric/capture/orchestrator.py",
        "src/novafabric/replay/_engine.py",
        "src/novafabric/lineage/_store.py",
        "examples/blackbox_demo/agent.py",
        "tests/fixtures/golden/capsule.json",
        "src/novafabric/schemas/data.json",
        "collector/go.mod",
        "deploy/helm/values.yaml",
        "Dockerfile",
    ],
)
def test_a_path_the_gate_cannot_scope_runs_the_full_suite(selected, path: str) -> None:
    tier, tests, reason = selected([path])
    assert tier == "full", f"{path} selected {tier}: {reason}"
    assert tests == ["*"]


def test_a_full_trigger_wins_even_beside_docs_only_paths(selected) -> None:
    tier, _, _ = selected(["docs/a.md", "README.md", "scripts/test-pool.sh"])
    assert tier == "full"


def test_no_change_runs_nothing(selected) -> None:
    assert selected([])[0] == "none"


def test_a_docs_change_runs_all_of_tests_docs_and_every_test_naming_it(selected) -> None:
    tier, tests, _ = selected(["docs/tutorials/feature-tour.md"])
    assert tier == "docs"
    assert {"tests/docs/test_a.py", "tests/docs/test_b.py"} <= set(tests)
    assert "tests/cli/test_reads_tour.py" in tests, "a test naming the changed path was dropped"
    assert "tests/cli/test_reads_docs_dir.py" in tests, 'a test naming "docs" was dropped'
    assert "tests/cli/test_unrelated.py" not in tests, "'docs' matched inside an unrelated word"


def test_the_docs_tier_leaves_out_what_make_test_fast_leaves_out(selected) -> None:
    _, tests, _ = selected(["docs/tutorials/feature-tour.md"])
    assert "tests/integration/test_names_tour.py" not in tests, "tests/integration is CI-only"
    assert not any(t.endswith("conftest.py") for t in tests)


@pytest.mark.parametrize(
    "path",
    ["README.md", "CHANGELOG.md", "ui/dashboard/src/pages/why.astro", "design/adr/0001-x.md"],
)
def test_text_and_front_end_paths_take_the_docs_tier(selected, path: str) -> None:
    tier, tests, _ = selected([path])
    assert tier == "docs"
    assert "tests/docs/test_a.py" in tests


def test_python_takes_the_scoped_tier_and_keeps_the_docs_set(selected) -> None:
    tier, tests, _ = selected(["src/novafabric/core.py", "docs/tutorials/feature-tour.md"])
    assert tier == "scoped"
    assert "tests/core/test_core.py" in tests, "the import closure was dropped"
    assert "tests/docs/test_a.py" in tests, "the scoped tier dropped tests/docs"
    assert "tests/cli/test_reads_tour.py" in tests, "Python suppressed the docs selection"


def test_a_selection_over_the_cap_runs_the_full_suite(selected) -> None:
    index = {f"tests/docs/test_{i}.py": [] for i in range(5)}
    index.update({f"tests/x/test_{i}.py": [] for i in range(5)})
    texts = dict.fromkeys(index, "")
    tier, _, reason = selected(["README.md"], index, texts)
    assert tier == "full", f"half the suite selected but tier {tier}: {reason}"


@pytest.mark.parametrize(
    ("tier", "require", "expected"),
    [
        ("docs", "branch", "docs"),
        ("scoped", "branch", "scoped"),
        ("none", "main", "none"),
        ("docs", "main", "docs"),
        ("scoped", "main", "full"),
        ("none", "full", "full"),
        ("docs", "full", "full"),
        ("scoped", "full", "full"),
        ("docs", "bogus", "full"),
    ],
)
def test_the_push_requirement_only_ever_raises_the_tier(
    testsel, tier: str, require: str, expected: str
) -> None:
    got, tests, _ = testsel.apply_requirement(tier, ["tests/docs/test_a.py"], "why", require)
    assert got == expected
    if expected == "full":
        assert tests == ["*"]


def test_gate_mode_prints_a_known_tier_first(testsel) -> None:
    out = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), str(SELECTOR), "--mode", "gate"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if out.returncode != 0 and "No such file" in out.stderr:
        pytest.skip("no .venv in this tree")
    assert out.returncode == 0, out.stderr
    first = out.stdout.splitlines()[0]
    assert first in testsel.GATE_TIERS, f"unknown tier line {first!r}"


# ── the stamp is writable in a git worktree ─────────────────────────────────────────

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "gate test",
    "GIT_AUTHOR_EMAIL": "gate-test@example.invalid",
    "GIT_COMMITTER_NAME": "gate test",
    "GIT_COMMITTER_EMAIL": "gate-test@example.invalid",
}


def _git(cwd: pathlib.Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | _GIT_ENV
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env
    )
    return proc.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_stamp_path_is_writable_inside_a_git_worktree(tmp_path: pathlib.Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "--template=", "-b", "main")
    empty_tree = _git(main, "hash-object", "-t", "tree", "--stdin", "-w")
    commit = _git(main, "commit-tree", empty_tree, "-m", "init")
    _git(main, "update-ref", "refs/heads/main", commit)
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "task", str(worktree), "main")
    assert (worktree / ".git").is_file(), "precondition: .git is a file in a worktree"

    (worktree / "scripts").mkdir()
    shutil.copy2(GATE, worktree / "scripts" / "test-gate.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["bash", "scripts/test-gate.sh", "--print-stamp-path"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    stamp = pathlib.Path(out.stdout.strip())
    assert stamp.parent.is_dir(), f"the stamp's directory {stamp.parent} is not a directory"
    stamp.write_text("digest docs\n")  # the old `.git/...` path raised NotADirectoryError
    assert stamp.read_text() == "digest docs\n"


# ── the pre-push hook reads the ref list ────────────────────────────────────────────


@pytest.fixture
def hook_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A copy of the hook beside a stub gate that records NOVA_GATE_REQUIRE."""
    (tmp_path / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy2(HOOK, tmp_path / "scripts" / "hooks" / "pre-push-test-gate.sh")
    stub = tmp_path / "scripts" / "test-gate.sh"
    stub.write_text('#!/usr/bin/env bash\necho "$NOVA_GATE_REQUIRE" > require.txt\n')
    stub.chmod(0o755)
    return tmp_path


Z = "0" * 40
A = "a" * 40


@pytest.mark.parametrize(
    ("stdin", "expected"),
    [
        (f"refs/heads/task {A} refs/heads/task {Z}\n", "branch"),
        (f"refs/heads/main {A} refs/heads/main {Z}\n", "main"),
        (f"refs/tags/v1 {A} refs/tags/v1 {Z}\n", "full"),
        (f"refs/heads/main {A} refs/heads/main {Z}\nrefs/tags/v1 {A} refs/tags/v1 {Z}\n", "full"),
        (f"(delete) {Z} refs/heads/main {A}\nrefs/heads/t {A} refs/heads/t {Z}\n", "branch"),
        ("", "full"),
    ],
)
def test_the_hook_derives_what_the_push_accepts(
    hook_tree: pathlib.Path, stdin: str, expected: str
) -> None:
    env = {k: v for k, v in os.environ.items() if k != "NOVA_SKIP_TEST_GATE"}
    proc = subprocess.run(
        ["bash", "scripts/hooks/pre-push-test-gate.sh"],
        cwd=hook_tree,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert (hook_tree / "require.txt").read_text().strip() == expected
