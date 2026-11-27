#!/usr/bin/env python3
"""Select the test files a change can affect, by static import closure.

Why this exists
---------------
`pytest-testmon` is the usual answer to "run only what my diff affects", and this
repo documented it as the inner loop. Measured 2026-09-01 on this tree it takes
**over 10 minutes** on both a cold and a warm index, because testmon cannot run
under `pytest-xdist`: its baseline is a *serial* run of a 12,280-test suite. So the
documented sub-second inner loop does not exist, and there was no feedback tier
below `make test-fast` (~4 min).

This selector fills that gap statically. It parses the import graph once (AST, no
imports executed, no code run), then maps changed files to the test files whose
transitive import closure touches them.

The safety rule
---------------
**Ambiguity always selects MORE, never less.** A selector that under-selects gives
false confidence, which is worse than no selector at all — this repo has been bitten
by exactly that (an `--ignore=<dir>` that silently discarded 73 tests). So anything
this cannot reason about — a changed `pyproject.toml`, a root `conftest.py`, a
non-Python file, a parse error — escalates to "run everything".

What it cannot see
------------------
Static analysis cannot follow a string-referenced entrypoint, a plugin registered by
name, or a runtime `importlib` call. That is why this tier is a **fast pre-check**,
not a merge gate: the full suite still runs on pre-push and in CI. Used that way it
is exactly the "test impact analysis" pattern large monorepos apply.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "novafabric"
TESTS = REPO / "tests"
PKG = "novafabric"

#: A change to any of these can affect anything, so they escalate to the full suite.
GLOBAL_TRIGGERS = {
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "tests/conftest.py",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
}

SENTINEL_ALL = "*"


def _module_name(path: Path) -> str | None:
    """`src/novafabric/lineage/backends/kuzu.py` -> `novafabric.lineage.backends.kuzu`."""
    try:
        rel = path.resolve().relative_to(SRC)
    except ValueError:
        return None
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join([PKG, *parts]) if parts else PKG


def _imported_modules(path: Path) -> set[str]:
    """Every `novafabric.*` module named by an import in *path* (AST only)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    except SyntaxError:
        # Unparseable file: cannot reason about it, so let the caller escalate.
        raise
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PKG or alias.name.startswith(PKG + "."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the package
                continue
            mod = node.module or ""
            if mod == PKG or mod.startswith(PKG + "."):
                found.add(mod)
                for alias in node.names:
                    found.add(f"{mod}.{alias.name}")
    return found


def build_src_graph() -> dict[str, set[str]]:
    """module -> the `novafabric.*` modules it imports directly."""
    graph: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        name = _module_name(path)
        if name is None:
            continue
        try:
            graph[name] = _imported_modules(path)
        except SyntaxError:
            graph[name] = set()
    return graph


def _closure(seeds: Iterable[str], graph: dict[str, set[str]]) -> set[str]:
    """Transitive import closure, tolerant of names that are not real modules.

    `from novafabric.lineage.store import NodeRow` yields the candidate
    `novafabric.lineage.store.NodeRow`, which is a symbol rather than a module. Such
    names simply have no edges, and any prefix of them that IS a module is added, so
    a symbol import still pins the module it came from.
    """
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        # A symbol import pins its defining module, and a submodule pins its parents.
        parts = name.split(".")
        for i in range(1, len(parts)):
            prefix = ".".join(parts[:i])
            if prefix.startswith(PKG) and prefix not in seen:
                stack.append(prefix)
        stack.extend(graph.get(name, ()))
    return seen


def build_index() -> dict[str, list[str]]:
    """test file (repo-relative) -> the src modules it can reach."""
    graph = build_src_graph()
    index: dict[str, list[str]] = {}
    for path in TESTS.rglob("*.py"):
        if not (path.name.startswith("test_") or path.name == "conftest.py"):
            continue
        rel = str(path.resolve().relative_to(REPO))
        try:
            direct = _imported_modules(path)
        except SyntaxError:
            index[rel] = [SENTINEL_ALL]
            continue
        index[rel] = sorted(_closure(direct, graph))
    return index


def _gitdir() -> str:
    """Prefer the PRIVATE gitdir: this repo is dual-git and it tracks the superset.

    The public `.git` is a curated subset whose `.git/info/exclude` hides private
    paths, so a change to a private-only Python file is invisible there — and a
    change the selector cannot see is a change it silently does not test.
    """
    return "--git-dir=.git-private" if Path(REPO / ".git-private").exists() else "--git-dir=.git"


def changed_files(base: str | None = None) -> list[str]:
    """Changed paths: the working tree by default, or a diff against *base*."""
    if base:
        cmd = ["git", _gitdir(), "diff", "--name-only", base]
    else:
        cmd = ["git", _gitdir(), "status", "--porcelain"]
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False).stdout
    if base:
        return [line.strip() for line in out.splitlines() if line.strip()]
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            paths.append(line[3:].strip().split(" -> ")[-1])
    return paths


def _tests_module(path: Path) -> list[str]:
    """`tests/a/b/_h.py` -> `["a", "b", "_h"]`; a package `__init__.py` -> its package."""
    parts = list(path.relative_to(REPO / "tests").with_suffix("").parts)
    return parts[:-1] if parts and parts[-1] == "__init__" else parts


def _tests_imports(path: Path) -> set[str] | None:
    """Every module *path* can import, in `pythonpath = tests` form; None if unparseable.

    Covers `import a.b._h`, `from a.b._h import x`, `from a.b import _h`
    (recorded as `a.b._h`), multi-line and relative imports, and the
    `tests.a.b._h` spelling some files use.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    except (SyntaxError, ValueError, OSError):
        return None
    own = _tests_module(path)
    package = own if path.name == "__init__.py" else own[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                base = ".".join([*package[:keep], *(node.module or "").split(".")]).strip(".")
            else:
                base = node.module or ""
            if base:
                names.add(base)
            names.update(f"{base}.{alias.name}".strip(".") for alias in node.names)
    return {n.removeprefix("tests.") for n in names}


def _dependents_of_test_modules(changed: list[str], index: dict[str, list[str]]) -> set[str]:
    """Test files that can import any of *changed* (repo-relative `.py` paths under
    tests/), directly or through other helpers and conftests.

    A conftest that imports a changed module brings in its whole subtree, and a
    helper that imports one is followed in turn. A file that cannot be parsed counts
    as an importer: ambiguity selects more, never less.
    """
    # REPO / "tests", not TESTS: resolved at call time, so a test can point REPO elsewhere.
    graph = {f: _tests_imports(f) for f in (REPO / "tests").rglob("*.py")}
    pending = [".".join(_tests_module(REPO / p)) for p in changed]
    pending = [d for d in pending if d]
    done: set[str] = set()
    matched: set[Path] = set()
    selected: set[str] = set()
    while pending:
        target = pending.pop()
        if target in done:
            continue
        done.add(target)
        for f, names in graph.items():
            if f in matched:
                continue
            prefix = target + "."
            if names is not None and not any(n == target or n.startswith(prefix) for n in names):
                continue
            matched.add(f)
            rel = str(f.relative_to(REPO))
            if f.name.startswith("test_"):
                selected.add(rel)
            elif f.name == "conftest.py":
                subtree = str(Path(rel).parent) + "/"
                selected |= {t for t in index if t.startswith(subtree)}
            dotted = ".".join(_tests_module(f))
            if dotted:
                pending.append(dotted)
    return selected


def select(paths: list[str], index: dict[str, list[str]]) -> tuple[list[str], str]:
    """Return (test files to run, human-readable reason)."""
    if not paths:
        return [], "no changed files"

    changed_modules: set[str] = set()
    selected: set[str] = set()
    tests_py: list[str] = []

    for raw in paths:
        p = raw.strip()
        if not p:
            continue
        if p in GLOBAL_TRIGGERS:
            return [SENTINEL_ALL], f"{p} changed — blast radius is the whole suite"
        if p.startswith("tests/"):
            if not p.endswith(".py"):
                continue
            if Path(p).name.startswith("test_"):
                selected.add(p)
            else:
                # A conftest governs its whole subtree. A helper module (`_harness.py`,
                # `__init__.py`, a fixture factory) is not a test file: handed to pytest it
                # collects nothing, or crashes when it reads its environment at import,
                # while the tests that use it go unrun. Select its subtree too (it may be
                # used by file path, not import), and below, everything that imports it.
                subtree = str(Path(p).parent) + "/"
                selected |= {t for t in index if t.startswith(subtree)}
            tests_py.append(p)
            continue
        if p.startswith("src/novafabric/") and p.endswith(".py"):
            name = _module_name(REPO / p)
            if name:
                changed_modules.add(name)
            continue
        if p.endswith(".py"):
            return [SENTINEL_ALL], f"{p} is Python outside src/tests — cannot scope it"
        # Non-Python (docs, schemas, workflows): no test mapping, select nothing
        # for it, but never let it suppress another path's selection.

    if tests_py:
        selected |= _dependents_of_test_modules(tests_py, index)

    if changed_modules:
        for test, reachable in index.items():
            if reachable == [SENTINEL_ALL] or changed_modules & set(reachable):
                selected.add(test)

    if not selected:
        return [], "no test file imports the changed code"
    return sorted(selected), f"{len(selected)} test file(s) reachable from the change"



#: If the import closure selects more than this share of the suite, the selection has
#: stopped being a saving. `lineage/__init__.py` eagerly imports every backend, so a
#: one-backend change legitimately reaches 55% of all test files — at which point
#: running the whole fast suite is simpler and strictly safer.
IMPACT_CAP = 0.40


def select_direct(paths: list[str]) -> tuple[list[str], str]:
    """Tier 0: the tests that obviously cover *paths*, by location and by name.

    Deliberately narrow. This is the "did I just break the thing I am editing"
    check that runs on every edit, so it must finish in seconds. It is NOT a
    safety gate — the closure tier and the full suite are.
    """
    selected: set[str] = set()
    reasons: list[str] = []
    all_tests = [str(p.relative_to(REPO)) for p in TESTS.rglob("test_*.py")]

    for raw in paths:
        p = raw.strip()
        if p.startswith("tests/") and p.endswith(".py"):
            if Path(p).name.startswith("test_"):
                selected.add(p)
            continue
        if not (p.startswith("src/novafabric/") and p.endswith(".py")):
            continue

        rel = Path(p).relative_to("src/novafabric")
        stem = rel.stem.lstrip("_")
        pkg = rel.parts[0] if len(rel.parts) > 1 else None

        # 1. a test directory named after the package
        if pkg:
            for t in all_tests:
                if t.startswith(f"tests/{pkg}/"):
                    selected.add(t)
                    reasons.append(f"tests/{pkg}/")
        # 2. any test file whose name mentions the module stem
        if len(stem) >= 4:
            for t in all_tests:
                if stem in Path(t).stem:
                    selected.add(t)
                    reasons.append(f"*{stem}*")

    if not selected:
        return [], "no directly matching test file"
    what = ", ".join(sorted(set(reasons))[:3]) or "name match"
    return sorted(selected), f"{len(selected)} test file(s) directly covering the change ({what})"


# ── Gate tiers: what the push gate (scripts/test-gate.sh) runs ────────────────────────
#
# The push gate used to run the whole fast suite for every push, so a one-line Markdown
# edit paid the same ~30 minutes as a schema change. `select_gate` picks the smallest
# tier that can still see the change. The safety rule is the one above, applied harder:
# a path the gate cannot classify runs everything, and every non-full tier still runs
# all of tests/docs (the guards that read docs, workflows, scripts and source text).
#
#   none    nothing differs from the base             -> run nothing
#   docs    only text/asset paths changed             -> tests/docs + tests naming those paths
#   scoped  Python in src/ or tests/ changed (+text)  -> the docs set + the import closure
#   full    anything else                             -> make test-fast
#
# CI still runs the full suite on every PR and on main; the gate decides only what has
# to pass on this machine before a push.

GATE_TIERS = ("none", "docs", "scoped", "full")

#: The gate scripts, CI config, schemas and the dual-git classification can change what
#: every test means, so a change under any of them runs the whole fast suite.
GATE_FULL_PREFIXES = ("scripts/", ".github/", "schemas/", ".dualgit/")

#: Central modules most subsystems depend on through runtime wiring the import graph
#: under-reports. A change to one of them runs the whole fast suite.
GATE_FULL_MODULES = {
    "src/novafabric/capture/orchestrator.py",
    "src/novafabric/replay/_engine.py",
    "src/novafabric/lineage/_store.py",
}

#: Text and front-end paths the Python suite reads only through tests that name them
#: (Markdown anywhere outside src/ and tests/ counts too).
#: The maintainers' private trees, present only in their dual-git working tree.
_PRIVATE_TEXT = ("design/", "papers/", ".claude/")  # private: never in the public repo
GATE_TEXT_PREFIXES = ("docs/", "ui/dashboard/", *_PRIVATE_TEXT)


def _test_texts(index: dict[str, list[str]]) -> dict[str, str]:
    """test file -> its source text, for the "does this test name that path" check."""
    texts: dict[str, str] = {}
    for rel in index:
        try:
            texts[rel] = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return texts


def _mentioning(path: str, texts: dict[str, str]) -> set[str]:
    """Test files whose source names *path*, its basename or any directory above it.

    Over-matches on purpose: a test that names `docs/`, `"docs"`, `docs/tutorials` or
    `feature-tour.md` is selected for a change to `docs/tutorials/feature-tour.md`.
    A one-component token is matched only as `"x"`, `'x'` or `x/`, so `docs` does not
    match every word that merely contains it.
    """
    parts = path.split("/")
    needles = {"/".join(parts[:i]) for i in range(2, len(parts) + 1)}
    needles.add(parts[-1])
    first = re.compile(rf"""["']{re.escape(parts[0])}["'/]|{re.escape(parts[0])}/""")
    return {
        rel
        for rel, text in texts.items()
        if any(n in text for n in needles) or (len(parts) > 1 and first.search(text))
    }


def select_gate(
    paths: list[str],
    index: dict[str, list[str]],
    texts: dict[str, str] | None = None,
    is_file: Callable[[str], bool] | None = None,
) -> tuple[str, list[str], str]:
    """Return (tier, test files to run, reason) for the push gate. See GATE_TIERS."""
    if is_file is None:
        is_file = lambda rel: (REPO / rel).is_file()  # noqa: E731
    changed = sorted({p.strip() for p in paths if p.strip()})
    if not changed:
        return "none", [], "nothing differs from the base"

    def full(why: str) -> tuple[str, list[str], str]:
        return "full", [SENTINEL_ALL], why

    python_paths: list[str] = []
    text_paths: list[str] = []
    for p in changed:
        if p in GLOBAL_TRIGGERS or p.startswith(GATE_FULL_PREFIXES) or p in GATE_FULL_MODULES:
            return full(f"{p} changed — the gate runs the whole fast suite for it")
        if p.endswith(".py"):
            if p.startswith(("src/novafabric/", "tests/")):
                python_paths.append(p)
                continue
            return full(f"{p} is Python outside src/tests — cannot scope it")
        if p.startswith(("src/", "tests/")):
            # Package data and fixtures are opened at runtime by computed paths.
            return full(f"{p} is a non-Python file under src/ or tests/ — cannot scope it")
        if p.endswith(".md") or p.startswith(GATE_TEXT_PREFIXES):
            text_paths.append(p)
            continue
        return full(f"{p} is outside every path class the gate can scope")

    if texts is None:
        texts = _test_texts(index)
    selected = {t for t in index if t.startswith("tests/docs/")}
    for p in text_paths:
        selected |= _mentioning(p, texts)

    tier = "docs"
    if python_paths:
        tier = "scoped"
        closure, why = select(python_paths, index)
        if closure == [SENTINEL_ALL]:
            return full(why)
        selected |= set(closure)

    if index and len(selected) > IMPACT_CAP * len(index):
        return full(
            f"{len(selected)} of {len(index)} test files selected — over the "
            f"{IMPACT_CAP:.0%} cap, so the whole fast suite is simpler and strictly safer"
        )
    # conftest.py files load on their own; a deleted test file has nothing to run;
    # tests/integration is CI-only, exactly as `make test-fast` leaves it out.
    runnable = sorted(
        t
        for t in selected
        if Path(t).name != "conftest.py"
        and not t.startswith("tests/integration/")
        and is_file(t)
    )
    reason = f"{tier}: {len(runnable)} test file(s) for {len(changed)} changed path(s)"
    return tier, runnable, reason


#: What a push accepts (scripts/hooks/pre-push-test-gate.sh decides from the refs).
GATE_REQUIREMENTS = ("branch", "main", "full")


def apply_requirement(
    tier: str, tests: list[str], reason: str, require: str
) -> tuple[str, list[str], str]:
    """Upgrade a selection to what the push accepts.

    `full` (a tag push, or unknown refs) always runs everything; `main` never accepts
    the import-closure guess of `scoped`, but does accept `docs` and `none`; `branch`
    accepts every tier. An unknown requirement is treated as `full`.
    """
    if require not in GATE_REQUIREMENTS or require == "full":
        if tier == "full":
            return tier, tests, reason
        return "full", [SENTINEL_ALL], f"this push needs the full suite ({require}); {reason}"
    if require == "main" and tier == "scoped":
        return "full", [SENTINEL_ALL], f"a push to main never accepts the scoped tier; {reason}"
    return tier, tests, reason


_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _git(args: list[str], gitdir: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    prefix = ["git"] if gitdir is None else ["git", f"--git-dir={gitdir}", "--work-tree=."]
    return subprocess.run(
        prefix + args, cwd=REPO, capture_output=True, text=True, check=False, env=env
    )


def gate_changed_files(base_ref: str = "origin/main") -> list[str] | None:
    """Paths that differ from the merge base with *base_ref*: committed, staged, unstaged
    and untracked-but-not-ignored. Renames are listed as delete + add.

    In the dual-git tree the private gitdir's changes are added too, because the public
    `.git` hides private paths. Returns None when the base cannot be found — the caller
    then runs everything.
    """
    gitdirs: list[str | None] = [None]
    if (REPO / ".git-private").is_dir():
        gitdirs.append(".git-private")
    paths: set[str] = set()
    for gitdir in gitdirs:
        base = _git(["merge-base", "HEAD", base_ref], gitdir)
        if base.returncode != 0 or not base.stdout.strip():
            return None
        diff = _git(["diff", "--no-renames", "--name-only", base.stdout.strip()], gitdir)
        new = _git(["ls-files", "--others", "--exclude-standard"], gitdir)
        if diff.returncode != 0 or new.returncode != 0:
            return None
        paths |= {line.strip() for line in diff.stdout.splitlines() + new.stdout.splitlines()}
    return sorted(p for p in paths if p)


def main() -> int:
    args = sys.argv[1:]
    base = None
    if "--base" in args:
        base = args[args.index("--base") + 1]

    if "--mode" in args and args[args.index("--mode") + 1] == "gate":
        # Always a fresh index: the gate must never select from a stale import graph.
        index = build_index()
        (REPO / ".testsel-index.json").write_text(json.dumps(index))
        require = args[args.index("--require") + 1] if "--require" in args else "branch"
        changed = gate_changed_files(base or "origin/main")
        if changed is None:
            tier, tests, reason = "full", [SENTINEL_ALL], "no merge base with the base ref"
        else:
            tier, tests, reason = select_gate(changed, index)
        tier, tests, reason = apply_requirement(tier, tests, reason, require)
        print(reason, file=sys.stderr)
        print(tier)
        for t in tests:
            print(t)
        return 0

    index_path = REPO / ".testsel-index.json"
    if "--rebuild-index" in args or not index_path.exists():
        index = build_index()
        index_path.write_text(json.dumps(index))
    else:
        index = json.loads(index_path.read_text())

    if "--build-only" in args:
        print(f"indexed {len(index)} test files", file=sys.stderr)
        return 0

    paths = changed_files(base)
    mode = args[args.index("--mode") + 1] if "--mode" in args else "impact"

    if mode == "direct":
        tests, reason = select_direct(paths)
    else:
        tests, reason = select(paths, index)
        if tests != [SENTINEL_ALL] and index and len(tests) > IMPACT_CAP * len(index):
            share = 100 * len(tests) / len(index)
            tests, reason = (
                [SENTINEL_ALL],
                f"{len(tests)} of {len(index)} test files ({share:.0f}%) reachable — "
                f"over the {IMPACT_CAP:.0%} cap, so running the full fast suite is "
                f"simpler and strictly safer",
            )

    print(reason, file=sys.stderr)
    if tests == [SENTINEL_ALL]:
        print(SENTINEL_ALL)
    else:
        for t in tests:
            print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
