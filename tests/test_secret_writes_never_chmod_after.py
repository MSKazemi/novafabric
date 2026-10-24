"""No secret is created at the umask mode and narrowed afterwards.

``path.write_text(secret); path.chmod(0o600)`` leaves the secret readable by
every local user between the two calls — and for good if the process dies
between them. Line-by-line review Wave 2 found that shape in the token files,
the promote keyring, the NovaSeal ratchet, ``nova login`` credentials and
backup restore. The fix is to create the file at 0600 from its first byte:
:func:`novafabric._safe_path.write_private_file` for bytes in hand,
:func:`novafabric._safe_path.ensure_private_file` for a file another library
opens (SQLite, an append log).

This guard reads ``src/novafabric`` with :mod:`ast` and fails on any
``chmod``/``os.chmod`` to an owner-only file mode (``0o600``, or
``S_IRUSR | S_IWUSR``) outside :data:`ALLOWED` — places that only *repair* an
existing file's mode, or narrow something that cannot be created at that mode.
Directory modes (``0o700``) are not this guard's concern.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "novafabric"

#: ``<file relative to src/novafabric>::<function>`` → why a chmod to 0600 is right there.
ALLOWED: dict[str, str] = {
    "serve/token_store.py::harden": (
        "repairs the mode of a store an older version left wider; never the first write"
    ),
    "trust/novaseal/local_identity.py::_tighten_key_mode": (
        "narrows an existing signing key found wider than 0600; keys are written 0600"
    ),
    "daemon/server.py::CaptureDaemon.bind": (
        "a socket cannot be created at a mode; it is bound inside a 0700 directory and "
        "peers are checked by uid"
    ),
}


def _is_owner_only_file_mode(node: ast.expr) -> bool:
    text = ast.unparse(node)
    if "0o600" in text or text in {"384", "_SECRET_MODE", "_KEY_MODE"}:
        return True
    return "S_IRUSR" in text and "S_IWUSR" in text and "S_IX" not in text and "RWX" not in text


def _chmod_mode(call: ast.Call) -> ast.expr | None:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "chmod":
        return None
    if isinstance(func.value, ast.Name) and func.value.id == "os":
        return call.args[1] if len(call.args) > 1 else None
    return call.args[0] if call.args else None


def _sites() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, prefix: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    name = f"{prefix}{child.name}"
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Call):
                            mode = _chmod_mode(sub)
                            if mode is not None and _is_owner_only_file_mode(mode):
                                found.append(f"{rel}::{name}")
                                break
                    visit(child, f"{name}.")
                elif isinstance(child, ast.ClassDef):
                    visit(child, f"{prefix}{child.name}.")

        visit(tree, "")
    return sorted(set(found))


def test_no_secret_is_narrowed_to_0600_after_it_was_written() -> None:
    offenders = [s for s in _sites() if s not in ALLOWED]
    assert not offenders, (
        "these functions chmod a file to 0600 after creating it, so it is readable at the "
        "umask mode meanwhile; write it with novafabric._safe_path.write_private_file, or "
        "create it with ensure_private_file before another library opens it:\n  "
        + "\n  ".join(offenders)
    )


def test_every_allowance_still_names_a_chmod_site() -> None:
    sites = set(_sites())
    stale = sorted(k for k in ALLOWED if k not in sites)
    assert not stale, f"allowances with no 0600 chmod left in them (remove them): {stale}"


def test_the_guard_sees_write_then_chmod() -> None:
    tree = ast.parse("def f(p):\n    p.write_text('k')\n    p.chmod(0o600)\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    modes = [m for c in calls if (m := _chmod_mode(c)) is not None]
    assert modes and _is_owner_only_file_mode(modes[0])
    tree = ast.parse("import os, stat\ndef g(d):\n    os.chmod(d, stat.S_IRWXU)\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not any(_is_owner_only_file_mode(m) for c in calls if (m := _chmod_mode(c)))
