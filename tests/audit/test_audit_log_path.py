"""The hash-chained audit log path resolves at CALL time, through one resolver.

Regression (2026-10-09): ``AUDIT_LOG_PATH`` was a module constant bound to
``~/.local/share/novafabric/audit.jsonl`` at import time and copied into ~25
modules with ``from novafabric.audit import AUDIT_LOG_PATH``. Only a handful of
call sites honoured ``NOVAFABRIC_AUDIT_LOG_PATH``, none honoured
``NOVAFABRIC_HOME``, so every ``nova export-evidence`` — including the ones the
test suite runs — appended to the developer's REAL audit log (it held
``test-user`` entries from test runs).
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.audit import AUDIT_LOG_PATH_ENV, resolve_audit_log_path
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.evidence.signing import generate_keypair

SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"

#: The account's real home directory, independent of any ``HOME`` override.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (AUDIT_LOG_PATH_ENV, "NOVAFABRIC_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)


# --- precedence -------------------------------------------------------------


def test_explicit_env_wins_over_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(tmp_path / "explicit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_audit_log_path() == tmp_path / "explicit.jsonl"


def test_novafabric_home_does_not_move_the_audit_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A custom data home must not hide the existing trail or fork a second chain."""
    _clear(monkeypatch)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_audit_log_path() == tmp_path / "xdg" / "novafabric" / "audit.jsonl"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    expected = tmp_path / "fakehome" / ".local" / "share" / "novafabric" / "audit.jsonl"
    assert resolve_audit_log_path() == expected


def test_xdg_data_home_when_no_novafabric_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_audit_log_path() == tmp_path / "xdg" / "novafabric" / "audit.jsonl"


def test_historical_default_when_nothing_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    expected = tmp_path / "fakehome" / ".local" / "share" / "novafabric" / "audit.jsonl"
    assert resolve_audit_log_path() == expected


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_values_count_as_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blank: str
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, blank)
    monkeypatch.setenv("NOVAFABRIC_HOME", blank)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_audit_log_path() == tmp_path / "xdg" / "novafabric" / "audit.jsonl"


def test_relative_xdg_data_home_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # XDG Base Directory spec: "If an implementation encounters a relative path in
    # any of these variables it should consider the path invalid and ignore it."
    _clear(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/xdg")
    assert resolve_audit_log_path().is_relative_to(tmp_path / "fakehome")


def test_resolves_at_call_time_not_import_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(tmp_path / "a.jsonl"))
    first = resolve_audit_log_path()
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(tmp_path / "b.jsonl"))
    assert (first, resolve_audit_log_path()) == (tmp_path / "a.jsonl", tmp_path / "b.jsonl")


def test_deprecated_alias_resolves_on_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novafabric.audit as audit_pkg
    from novafabric.audit import _paths

    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(tmp_path / "alias.jsonl"))
    assert audit_pkg.AUDIT_LOG_PATH == tmp_path / "alias.jsonl"
    assert _paths.AUDIT_LOG_PATH == tmp_path / "alias.jsonl"
    with pytest.raises(AttributeError):
        _ = audit_pkg.NOT_A_THING  # type: ignore[attr-defined]


# --- every call site goes through the resolver --------------------------------


def _import_time_bindings() -> list[str]:
    """Every place in src/ that names ``AUDIT_LOG_PATH`` outside the audit package."""
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.parent == SRC / "audit" and path.name in {"__init__.py", "_paths.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                a.name == "AUDIT_LOG_PATH" for a in node.names
            ):
                hits.append(f"{path.relative_to(SRC)}:{node.lineno} imports AUDIT_LOG_PATH")
            elif isinstance(node, ast.Attribute) and node.attr == "AUDIT_LOG_PATH":
                hits.append(f"{path.relative_to(SRC)}:{node.lineno} reads .AUDIT_LOG_PATH")
    return hits


def test_no_module_binds_the_audit_log_path_at_import_time() -> None:
    assert _import_time_bindings() == [], (
        "call novafabric.audit.resolve_audit_log_path() where the path is needed; "
        "an import-time binding ignores NOVAFABRIC_AUDIT_LOG_PATH / NOVAFABRIC_HOME"
    )


def test_no_module_hardcodes_the_default_audit_log_location() -> None:
    # A string constant (e.g. a CLI option default) naming the historical path
    # bypasses the resolver just as surely as an import-time binding. Prose in
    # docstrings is fine; a bare string value is not.
    offenders = [
        f"{p.relative_to(SRC)}:{node.lineno}"
        for p in sorted(SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().endswith(".local/share/novafabric/audit.jsonl")
    ]
    assert offenders == []


# --- behaviour: export-evidence writes where the environment says -------------


def _export(tmp_path: Path) -> None:
    # ``app`` (and with it evidence/bundle.py) is imported at MODULE level, i.e.
    # at collection time, before any monkeypatch below: a module that binds the
    # audit path at import time is then caught, not hidden by a late import.
    capsule = (
        CaptureOrchestrator(base_dir=tmp_path / "runs")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )
    priv, _ = generate_keypair(tmp_path / "keys")
    result = CliRunner().invoke(
        app,
        ["export-evidence", str(capsule), "--key", str(priv), "--output", str(tmp_path / "b.zip")],
    )
    assert result.exit_code == 0, result.output


def _actions(path: Path) -> list[str]:
    return [json.loads(line)["event_type"] for line in path.read_text().splitlines() if line]


def test_export_evidence_audits_to_the_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "override" / "audit.jsonl"
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(target))
    _export(tmp_path)
    assert "policy.allow" in _actions(target)


def test_export_evidence_audits_under_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(AUDIT_LOG_PATH_ENV, raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nhome"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    _export(tmp_path)
    assert "policy.allow" in _actions(tmp_path / "xdg" / "novafabric" / "audit.jsonl")
    assert not (tmp_path / "nhome" / "audit.jsonl").exists()


def test_export_evidence_refuses_cleanly_when_the_audit_log_is_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unwritable = tmp_path / "audit-is-a-directory"
    unwritable.mkdir()
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(unwritable))
    capsule = (
        CaptureOrchestrator(base_dir=tmp_path / "runs")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )
    priv, _ = generate_keypair(tmp_path / "keys")
    out = tmp_path / "b.zip"
    result = CliRunner().invoke(
        app, ["export-evidence", str(capsule), "--key", str(priv), "--output", str(out)]
    )
    assert result.exit_code == 1, result.output
    assert "audit log" in result.output
    assert not out.exists(), "an export whose policy decision was not audited was written"


# --- the suite itself can never reach the real audit log ----------------------


def test_the_suite_never_resolves_the_audit_log_under_the_real_home() -> None:
    # tests/conftest.py::_hermetic_novafabric_env points every layer of the
    # precedence at a per-test tmp dir. If this fails, a test run WILL append
    # to the developer's real audit log.
    assert not resolve_audit_log_path().resolve().is_relative_to(REAL_HOME / ".local")
    assert not resolve_audit_log_path().resolve().is_relative_to(REAL_HOME / ".novafabric")


def test_the_suite_stays_off_the_real_home_even_without_the_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defence in depth: drop the explicit override; XDG_DATA_HOME from the
    # hermetic fixture must still keep us off it (NOVAFABRIC_HOME never moves it).
    monkeypatch.delenv(AUDIT_LOG_PATH_ENV, raising=False)
    assert not resolve_audit_log_path().resolve().is_relative_to(REAL_HOME / ".local")
    monkeypatch.delenv("NOVAFABRIC_HOME", raising=False)
    assert not resolve_audit_log_path().resolve().is_relative_to(REAL_HOME / ".local")
