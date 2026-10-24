"""Every rename that publishes a file or directory makes the rename durable — or says why not.

A rename is atomic for visibility, not for durability: after a crash on XFS or
Lustre the old name can come back, or the new one can point at bytes that never
reached the disk, unless the directory is fsynced after the rename (and a
renamed file was fsynced before). Line-by-line review X2 found the legal-hold
ledger rewrite, media blobs, the token store and others skipping it; for
``holds.jsonl`` an empty file reads as *no holds*, so deletion failed open.

This guard reads ``src/novafabric`` with :mod:`ast`. A function that calls
``os.replace``, ``os.rename``, ``shutil.move`` or ``<path>.rename`` must also
fsync a directory in the same function — :func:`novafabric._safe_path.fsync_dir`,
:func:`novafabric.capsule._atomic.atomic_replace`, or ``os.fsync`` on a
directory descriptor (a name containing ``dir``) — or it is listed in
:data:`NOT_DURABLE_BY_DESIGN` with the reason. Writers that hand their bytes to
:func:`~novafabric._safe_path.atomic_write_bytes` /
:func:`~novafabric._safe_path.write_private_file` /
:func:`~novafabric._capsule_fs.replace_capsule_file` never appear here: they
rename nothing themselves. ``tests/test_durable_writes.py`` checks the order of
the steps for the important writers.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "novafabric"

_RENAME_CALLS = {("os", "replace"), ("os", "rename"), ("shutil", "move")}
_DURABLE_CALLS = {"fsync_dir", "atomic_replace"}

#: ``<file relative to src/novafabric>::<function>`` → why its rename need not be durable.
NOT_DURABLE_BY_DESIGN: dict[str, str] = {}


def _call_name(node: ast.Call) -> tuple[str | None, str | None]:
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _is_rename(node: ast.Call) -> bool:
    owner, attr = _call_name(node)
    if (owner, attr) in _RENAME_CALLS:
        return True
    # ``<path>.rename(target)``: Path.rename. str has no .rename, so this is a path.
    return attr == "rename" and owner not in {"os", "shutil"} and len(node.args) == 1


def _is_dir_fsync(node: ast.Call) -> bool:
    owner, attr = _call_name(node)
    if attr in _DURABLE_CALLS:
        return True
    if (owner, attr) == ("os", "fsync") and node.args:
        return "dir" in ast.unparse(node.args[0]).lower()
    return False


def _own_calls(fn: ast.AST) -> list[ast.Call]:
    """Calls in *fn*'s body, not inside a nested function or class."""
    out: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _rename_sites() -> dict[str, bool]:
    """``file::qualname`` of every function that renames → whether it fsyncs a directory."""
    sites: dict[str, bool] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, prefix: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    name = f"{prefix}{child.name}"
                    calls = _own_calls(child)
                    if any(_is_rename(c) for c in calls):
                        sites[f"{rel}::{name}"] = any(_is_dir_fsync(c) for c in calls)
                    visit(child, f"{name}.")
                elif isinstance(child, ast.ClassDef):
                    visit(child, f"{prefix}{child.name}.")

        visit(tree, "")
    return sites


def test_every_rename_site_is_durable_or_explained() -> None:
    sites = _rename_sites()
    undurable = sorted(
        site for site, durable in sites.items() if not durable and site not in NOT_DURABLE_BY_DESIGN
    )
    assert not undurable, (
        "these functions rename without fsyncing the directory afterwards; use "
        "novafabric._safe_path.atomic_write_bytes / write_private_file, or call fsync_file on "
        "the source before and fsync_dir on the target's directory after the rename — or add "
        "the site to NOT_DURABLE_BY_DESIGN with the reason:\n  " + "\n  ".join(undurable)
    )


def test_every_exemption_names_a_real_rename_site() -> None:
    sites = _rename_sites()
    stale = sorted(k for k in NOT_DURABLE_BY_DESIGN if k not in sites)
    assert not stale, f"exemptions with no rename left in them (remove them): {stale}"
    reasons = [k for k, why in NOT_DURABLE_BY_DESIGN.items() if len(why.split()) < 4]
    assert not reasons, f"give each exemption a real reason: {reasons}"


def test_the_guard_sees_a_bare_rename() -> None:
    tree = ast.parse("import os\ndef f(a, b):\n    os.replace(a, b)\n")
    (fn,) = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    calls = _own_calls(fn)
    assert any(_is_rename(c) for c in calls)
    assert not any(_is_dir_fsync(c) for c in calls)
    tree = ast.parse("def f(p, q):\n    p.rename(q)\n    fsync_dir(q.parent)\n")
    (fn,) = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert any(_is_dir_fsync(c) for c in _own_calls(fn))
