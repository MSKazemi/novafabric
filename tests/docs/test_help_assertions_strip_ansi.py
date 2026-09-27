"""A `--flag` assertion must go through the ANSI-stripping helper.

This class has now cost two debugging rounds. Rich emits escape sequences
*inside* option names when colour is enabled, so ``"--flag" in result.output``
is False even though a human reading the help sees the flag plainly. CI enables
colour; pytest's capture makes Rich disable it locally, so the failure appears
only in CI and is unreproducible by hand.

* 2026-08-05 (issue #21): six such assertions. `tests/_help_assert.py` was
  written to fix them and documents the diagnosis.
* 2026-09-27: nine more arrived with the accepted-ADR slice batches and turned
  the `unit` job red. The helper existed the whole time; nothing required it.

So the rule is enforced rather than remembered: an assertion that a string
starting with ``--`` appears in a result's ``.output``/``.stdout`` must use
``assert_flag_in_help`` or ``strip_ansi`` from ``tests/_help_assert.py``.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HELPER = "_help_assert"

#: Calls that render output as a reader sees it, escape sequences removed.
SANITISERS = {"strip_ansi", "_squash", "_plain", "_render"}


def _test_files() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "ls-files", "tests"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / p for p in out.splitlines() if p.endswith(".py")]


def _raw_flag_assertions(path: pathlib.Path) -> list[str]:
    """`"--x" in <expr>.output` where the file never imports the helper."""
    source = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    found: list[str] = []
    # Only assertions inside a test that actually invokes `--help`. Rich reliably
    # colourises the help renderer; ordinary command output is a broader, noisier
    # class that is not what turned CI red, and a guard that flags working code
    # is a guard people learn to suppress.
    help_functions = {
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(c, ast.Constant) and c.value == "--help"
            for c in ast.walk(fn)
        )
    }
    in_help_fn = {
        id(node)
        for fn in help_functions
        for node in ast.walk(fn)
    }
    # Output is often bound to a local first (`out = result.output`), which hides
    # the attribute from the comparator. Record, per variable, whether the value
    # it was bound to came from raw output and whether it was sanitised on the way.
    raw_vars: set[str] = set()
    for fn in help_functions:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            touches_output = any(
                isinstance(s, ast.Attribute) and s.attr in {"output", "stdout"}
                for s in ast.walk(node.value)
            )
            if not touches_output:
                continue
            sanitised = any(
                isinstance(c, ast.Call)
                and isinstance(getattr(c, "func", None), ast.Name)
                and c.func.id in SANITISERS
                for c in ast.walk(node.value)
            )
            if sanitised:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    raw_vars.add(target.id)

    # `for flag in ("--a", "--b"): assert flag in out` is the other real shape —
    # trust_path's help test used exactly this and was one of the CI failures.
    flag_loop_vars: set[str] = set()
    for fn in help_functions:
        for node in ast.walk(fn):
            if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
                continue
            if not isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
                continue
            items = node.iter.elts
            if items and all(
                isinstance(e, ast.Constant)
                and isinstance(e.value, str)
                and e.value.startswith("--")
                for e in items
            ):
                flag_loop_vars.add(node.target.id)

    for node in ast.walk(tree):
        if id(node) not in in_help_fn:
            continue
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.In):
            continue
        left = node.left
        if isinstance(left, ast.Name) and left.id in flag_loop_vars:
            label = f"<loop flag {left.id!r}>"
        elif (
            isinstance(left, ast.Constant)
            and isinstance(left.value, str)
            and left.value.startswith("--")
        ):
            label = repr(left.value)
        else:
            continue
        for comparator in node.comparators:
            # sanitised if the raw output passes through something that removes
            # escape sequences — the helper itself, or a local wrapper built on it
            sanitised = any(
                isinstance(c, ast.Call)
                and isinstance(getattr(c, "func", None), ast.Name)
                and c.func.id in SANITISERS
                for c in ast.walk(comparator)
            )
            if sanitised:
                continue
            rel = path.relative_to(REPO_ROOT)
            if isinstance(comparator, ast.Name) and comparator.id in raw_vars:
                found.append(
                    f"{rel}:{node.lineno}  {label} in {comparator.id}"
                    " (bound to unsanitised output)"
                )
                continue
            for sub in ast.walk(comparator):
                if isinstance(sub, ast.Attribute) and sub.attr in {"output", "stdout"}:
                    found.append(f"{rel}:{node.lineno}  {label} in ...{sub.attr}")
                    break
    return found


def test_no_raw_flag_assertion_against_coloured_output() -> None:
    offenders: list[str] = []
    for path in _test_files():
        offenders.extend(_raw_flag_assertions(path))

    assert not offenders, (
        "these assert a `--flag` against raw CLI output. Rich puts ANSI escapes "
        "INSIDE option names when colour is on, so this passes locally and fails "
        "in CI. Use assert_flag_in_help (or strip_ansi) from tests/_help_assert.py:"
        "\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_is_not_vacuous() -> None:
    files = _test_files()
    assert len(files) > 500, f"expected the test tree, found {len(files)} files"
    assert (REPO_ROOT / "tests" / "_help_assert.py").is_file()
