"""FR-07 / FR-13: static guards on the RLS primitives, in pure Python.

These guards used to shell out to ``grep -E`` and assert only on its exit
code. ``grep -E`` has no lookahead, so the FR-07 pattern ``(?!LOCAL…)`` was a
syntax error: grep printed a warning, exited non-zero, and the test — which
read "non-zero" as "no match" — passed even on a known-bad line. The FR-13
guard had the sibling flaw: run from any directory other than the repo root,
grep found no ``src/`` and exited 2 with empty output, which read as "no
violations".

Each guard is now a regex over the files read in Python, anchored to the repo
root via ``__file__``, and each carries a known-bad control proving it can fire.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "novafabric"
METADATA_STORE = SRC_ROOT / "metadata_store"

#: Known-bad fixture: a store that sets the tenant GUC with a session-scoped SET.
BROKEN_SESSION_SET = REPO_ROOT / "tests" / "fixtures" / "broken_session_set.py"

# A session-scoped tenant GUC: ``SET app.x`` or ``SET SESSION app.x`` (SQL is
# case-insensitive). ``SET LOCAL app.x`` is the only allowed form (FR-07).
_SESSION_SET = re.compile(r"\bSET\s+(?:SESSION\s+)?app\.", re.IGNORECASE)
# ``set_config('app.x', v, false)`` is the function spelling of the same thing.
_SESSION_SET_CONFIG = re.compile(
    r"\bset_config\s*\(\s*['\"]app\.[^'\"]*['\"]\s*,[^,)]*,\s*false\b", re.IGNORECASE
)

_RLS_PRIMITIVE = re.compile(r"current_setting\(.*app\.|FORCE\s+ROW\s+LEVEL")
_RLS_ALLOWED = (
    "src/novafabric/metadata_store/",
    "tests/metadata_store/",
    "tests/fixtures/broken_session_set.py",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def find_session_sets(paths: Iterable[Path]) -> list[str]:
    """Return ``path:line: text`` for every session-scoped tenant GUC write."""
    hits: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SESSION_SET.search(line) or _SESSION_SET_CONFIG.search(line):
                hits.append(f"{path}:{lineno}: {line.strip()}")
    return hits


def find_rls_primitives_outside(root: Path, scan_dirs: Iterable[str]) -> list[str]:
    """Return ``relpath:line: text`` for RLS primitives outside the allowed paths."""
    violations: list[str] = []
    for scan_dir in scan_dirs:
        for path in _python_files(root / scan_dir):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(_RLS_ALLOWED):
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _RLS_PRIMITIVE.search(line):
                    violations.append(f"{rel}:{lineno}: {line.strip()}")
    return violations


# ── FR-07: no session-scoped SET of the tenant GUC ──────────────────────────


def test_no_session_set_in_src() -> None:
    """FR-07: no session-scoped ``SET app.`` (without LOCAL) anywhere in src/novafabric/."""
    files = _python_files(SRC_ROOT)
    # Non-vacuous: the scan really covers the file that sets the tenant GUC.
    assert METADATA_STORE / "postgres.py" in files
    hits = find_session_sets(files)
    assert not hits, (
        "Session-scoped tenant GUC found — must always use SET LOCAL:\n" + "\n".join(hits)
    )


def test_real_store_uses_set_local() -> None:
    """Positive control: the shipped store sets the GUC with SET LOCAL, and it passes."""
    postgres = METADATA_STORE / "postgres.py"
    assert "SET LOCAL app.current_tenant_id" in postgres.read_text(encoding="utf-8")
    assert find_session_sets([postgres]) == []


def test_guard_fires_on_known_bad_fixture() -> None:
    """Negative control: the session-SET mutant fixture is flagged on its SET line."""
    hits = find_session_sets([BROKEN_SESSION_SET])
    assert len(hits) == 1, hits
    assert "SET app.current_tenant_id" in hits[0]


@pytest.mark.parametrize(
    "line",
    [
        "conn.execute(\"SET app.current_tenant_id = 'x'\")",
        "conn.execute(\"set app.current_tenant_id = 'x'\")",
        "conn.execute(\"SET SESSION app.current_tenant_id = 'x'\")",
        "conn.execute(\"SET\tapp.current_tenant_id TO 'x'\")",
        "conn.execute(\"SELECT set_config('app.current_tenant_id', %s, false)\")",
        "conn.execute(\"SELECT SET_CONFIG('app.current_tenant_id', %s, FALSE)\")",
    ],
)
def test_guard_flags_session_scoped_forms(tmp_path: Path, line: str) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(line + "\n", encoding="utf-8")
    assert len(find_session_sets([bad])) == 1


@pytest.mark.parametrize(
    "line",
    [
        "conn.execute(\"SET LOCAL app.current_tenant_id = 'x'\")",
        "conn.execute(\"set local app.current_tenant_id = 'x'\")",
        "conn.execute(\"SELECT set_config('app.current_tenant_id', %s, true)\")",
        "conn.execute(\"SET statement_timeout = 0\")",
        "# the GUC must be set with SET LOCAL inside a transaction",
    ],
)
def test_guard_allows_transaction_scoped_forms(tmp_path: Path, line: str) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text(line + "\n", encoding="utf-8")
    assert find_session_sets([ok]) == []


# ── FR-13: RLS primitives confined to metadata_store/ ───────────────────────


def test_rls_strings_confined_to_metadata_store() -> None:
    """FR-13: current_setting(app.) and FORCE ROW LEVEL must not appear outside metadata_store/."""
    violations = find_rls_primitives_outside(REPO_ROOT, ("src", "tests"))
    assert not violations, "RLS primitives found outside allowed paths:\n" + "\n".join(violations)


def test_rls_guard_sees_the_real_primitives() -> None:
    """Non-vacuous: the pattern matches the shipped primitives inside metadata_store/."""
    text = (METADATA_STORE / "postgres.py").read_text(encoding="utf-8")
    assert _RLS_PRIMITIVE.search(text)


def test_rls_guard_fires_outside_allowed_paths(tmp_path: Path) -> None:
    """Negative control: a primitive planted outside metadata_store/ is reported."""
    planted = tmp_path / "src" / "novafabric" / "server" / "leak.py"
    planted.parent.mkdir(parents=True)
    planted.write_text('op.execute("ALTER TABLE runs FORCE ROW LEVEL SECURITY")\n', encoding="utf-8")
    allowed = tmp_path / "src" / "novafabric" / "metadata_store" / "ok.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("x = \"current_setting('app.current_tenant_id')\"\n", encoding="utf-8")
    violations = find_rls_primitives_outside(tmp_path, ("src",))
    assert violations == [
        'src/novafabric/server/leak.py:1: op.execute("ALTER TABLE runs FORCE ROW LEVEL SECURITY")'
    ]
