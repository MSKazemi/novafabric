"""ADR-0211 D5: `nova db upgrade --track registry|metadata` (unit tier).

The two-alembic-universe trap: `nova db upgrade` historically migrated ONLY
the MetadataStore tier while the runbooks told operators to run it after a
registry restore. `--track` disambiguates; the default (`metadata`) is
byte-identical to prior behavior. The registry sqlite track is exercised for
real against a tmp home; postgres registry-track migration runs in the
containers tier (tests/metadata_store/test_pg_restore_roundtrip.py).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.migrations.registry_track import script_head

runner = CliRunner()


def test_track_registry_sqlite_migrates_registry_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "nova-home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))

    result = runner.invoke(
        app, ["db", "upgrade", "--track", "registry", "--backend", "sqlite"]
    )
    assert result.exit_code == 0, result.output
    assert "registry" in result.output

    head = script_head("sqlite")
    conn = sqlite3.connect(home / "registry.db")
    try:
        stamp = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()
    assert stamp == head


def test_track_registry_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "nova-home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    for _ in range(2):
        result = runner.invoke(
            app, ["db", "upgrade", "--track", "registry", "--backend", "sqlite"]
        )
        assert result.exit_code == 0, result.output


def test_track_registry_postgres_without_dsn_exit2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)
    result = runner.invoke(
        app, ["db", "upgrade", "--track", "registry", "--backend", "postgres"]
    )
    assert result.exit_code == 2
    assert "NOVAFABRIC_POSTGRES_DSN" in result.output


def test_unknown_track_exit2() -> None:
    result = runner.invoke(app, ["db", "upgrade", "--track", "bogus"])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_track_registry_never_prints_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = "postgresql://nova:sekrit@127.0.0.1:1/nova"
    monkeypatch.setenv("NOVAFABRIC_POSTGRES_DSN", dsn)
    result = runner.invoke(
        app, ["db", "upgrade", "--track", "registry", "--backend", "postgres"]
    )
    # Unreachable target: the command fails, but the DSN never surfaces.
    assert result.exit_code == 1
    assert dsn not in result.output
    assert "sekrit" not in result.output


def test_default_track_is_metadata_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default path must remain the MetadataStore tier (byte-identical
    # behavior): it reads NOVAFABRIC_DB_PATH and runs the packaged
    # metadata_store alembic config via subprocess.
    calls: dict[str, object] = {}

    import novafabric.metadata_store.cli as ms_cli

    class _Proc:
        returncode = 0

    def _fake_run(argv, **kwargs):  # noqa: ANN001
        calls["argv"] = argv
        return _Proc()

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "metadata.db"))
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    argv = calls["argv"]
    assert argv[0] == "alembic"
    assert "metadata_store" in str(argv[2])  # the metadata track's alembic.ini
    assert ms_cli is not None
