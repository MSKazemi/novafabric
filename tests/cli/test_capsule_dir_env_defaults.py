"""``--capsule-dir`` defaults follow the store ``nova capture`` writes to.

``nova pii erase`` and ``nova pii status`` defaulted ``--capsule-dir`` to
``$NOVAFABRIC_HOME/capsules`` and ignored ``NOVAFABRIC_CAPSULE_DIR``, which
``nova capture`` honours. With the env var set (the Docker layout,
``/data/capsules``) an erasure scanned an empty directory, found no capsules for
the subject, and a status lookup by bare capsule id reported "not found".
``nova migrate-schema`` read the same rule, but once, when the module was
imported, as a Python default argument.

Precedence is the one ``nova retention`` / ``nova classify`` / ``nova backup``
already use: explicit flag > ``NOVAFABRIC_CAPSULE_DIR`` > ``$NOVAFABRIC_HOME/capsules``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _help_assert import strip_ansi
from typer.testing import CliRunner

import novafabric.cli.migrate_schema as migrate_schema_cli
import novafabric.cli.pii_erase as pii_erase_cli
from novafabric.cli.main import app

runner = CliRunner()


class _Stop(Exception):
    """Raised by a spy once it has seen the resolved directory."""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    return home


@pytest.fixture()
def env_store(tmp_path: Path) -> Path:
    store = tmp_path / "data" / "capsules"
    store.mkdir(parents=True)
    return store


# ---------------------------------------------------------------------------
# nova pii erase
# ---------------------------------------------------------------------------


def _erase_scans(monkeypatch: pytest.MonkeyPatch, *extra: str) -> Path:
    seen: dict[str, Path] = {}

    def spy(subject_id: str, capsule_dir: Path) -> Any:
        seen["capsule_dir"] = capsule_dir
        raise _Stop

    monkeypatch.setattr(pii_erase_cli, "_find_capsule_ids_for_subject", spy)
    result = runner.invoke(app, ["pii", "erase", "user@example.com", *extra])
    assert isinstance(result.exception, _Stop), (result.exception, result.output)
    return seen["capsule_dir"]


def test_pii_erase_follows_capsule_dir_env(
    home: Path, env_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(env_store))
    assert _erase_scans(monkeypatch) == env_store


def test_pii_erase_falls_back_to_home_capsules(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _erase_scans(monkeypatch) == home / "capsules"


def test_pii_erase_explicit_flag_wins(
    home: Path, env_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(env_store))
    flag = tmp_path / "flag"
    assert _erase_scans(monkeypatch, "--capsule-dir", str(flag)) == flag


# ---------------------------------------------------------------------------
# nova pii status — real lookup of a bare capsule id
# ---------------------------------------------------------------------------


def _status(capsule_id: str, *extra: str) -> Any:
    return runner.invoke(app, ["pii", "status", capsule_id, "--json", *extra])


def test_pii_status_finds_bare_id_in_capsule_dir_env(
    home: Path, env_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env_store / "01HSTATUSENV").mkdir()
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(env_store))
    result = _status("01HSTATUSENV")
    assert result.exit_code == 0, (result.exception, result.output)
    assert Path(json.loads(result.stdout)["capsule_dir"]) == env_store / "01HSTATUSENV"


def test_pii_status_falls_back_to_home_capsules(home: Path) -> None:
    (home / "capsules" / "01HSTATUSHOME").mkdir(parents=True)
    result = _status("01HSTATUSHOME")
    assert result.exit_code == 0, (result.exception, result.output)
    assert Path(json.loads(result.stdout)["capsule_dir"]) == home / "capsules" / "01HSTATUSHOME"


def test_pii_status_explicit_flag_wins(
    home: Path, env_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = tmp_path / "flag"
    (flag / "01HSTATUSFLAG").mkdir(parents=True)
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(env_store))
    result = _status("01HSTATUSFLAG", "--capsule-dir", str(flag))
    assert result.exit_code == 0, (result.exception, result.output)
    assert Path(json.loads(result.stdout)["capsule_dir"]) == flag / "01HSTATUSFLAG"


def test_pii_status_library_with_explicit_home_honours_env(
    home: Path, env_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.pii.status import build_pii_status

    (env_store / "01HSTATUSLIB").mkdir()
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(env_store))
    report = build_pii_status("01HSTATUSLIB", nova_home=tmp_path / "other-home")
    assert report.capsule_dir is not None
    assert Path(report.capsule_dir) == env_store / "01HSTATUSLIB"


# ---------------------------------------------------------------------------
# nova migrate-schema — resolved per call, not frozen at import
# ---------------------------------------------------------------------------


def _migrate_walks(monkeypatch: pytest.MonkeyPatch, *extra: str) -> Path:
    seen: dict[str, Path] = {}

    def spy(capsule_dir: Path, **kwargs: Any) -> Any:
        seen["capsule_dir"] = capsule_dir
        raise _Stop

    monkeypatch.setattr(migrate_schema_cli, "migrate_capsule_dir", spy)
    result = runner.invoke(app, ["migrate-schema", "--dry-run", *extra])
    assert isinstance(result.exception, _Stop), (result.exception, result.output)
    return seen["capsule_dir"]


def test_migrate_schema_follows_env_set_after_import(
    home: Path, env_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(env_store))
    assert _migrate_walks(monkeypatch) == env_store


def test_migrate_schema_falls_back_to_home_capsules(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _migrate_walks(monkeypatch) == home / "capsules"


def test_migrate_schema_explicit_flag_wins(
    home: Path, env_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(env_store))
    flag = tmp_path / "flag"
    assert _migrate_walks(monkeypatch, "--capsule-dir", str(flag)) == flag


# ---------------------------------------------------------------------------
# --help states the real default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ["pii", "erase"],
        ["pii", "status"],
        ["import"],
        ["serve"],
        ["migrate-schema"],
    ],
    ids=lambda c: " ".join(c),
)
def test_help_names_the_env_var(command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--help"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    text = " ".join(strip_ansi(result.output).split())
    assert "NOVAFABRIC_CAPSULE_DIR" in text, text
