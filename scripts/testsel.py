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
import subprocess
import sys
from collections.abc import Iterable
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


def select(paths: list[str], index: dict[str, list[str]]) -> tuple[list[str], str]:
    """Return (test files to run, human-readable reason)."""
    if not paths:
        return [], "no changed files"

    changed_modules: set[str] = set()
    selected: set[str] = set()

    for raw in paths:
        p = raw.strip()
        if not p:
            continue
        if p in GLOBAL_TRIGGERS:
            return [SENTINEL_ALL], f"{p} changed — blast radius is the whole suite"
        if p.startswith("tests/"):
            if Path(p).name == "conftest.py":
                # A conftest governs its whole subtree.
                subtree = str(Path(p).parent) + "/"
                selected |= {t for t in index if t.startswith(subtree)}
            elif p.endswith(".py"):
                selected.add(p)
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


def main() -> int:
    args = sys.argv[1:]
    base = None
    if "--base" in args:
        base = args[args.index("--base") + 1]

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
