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
from novafabric.metadata_store.cli import run_alembic_upgrade
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
    # The default path must remain the MetadataStore tier: it reads
    # NOVAFABRIC_DB_PATH and runs the packaged metadata_store alembic config
    # via subprocess. Since B13 the argv runs alembic through *this*
    # interpreter (`sys.executable -m alembic`) rather than the bare console
    # script, so the track assertion is on the ini path, not on argv[0].
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
    import sys

    argv = calls["argv"]
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "alembic"]
    # the metadata track's alembic.ini, still the default track
    assert "metadata_store" in str(argv[argv.index("-c") + 1])
    assert ms_cli is not None


# ---------------------------------------------------------------------------
# B13 (campaign 3): `nova db upgrade` must not depend on `alembic` being on PATH
# ---------------------------------------------------------------------------
#
# Root cause found on a live fleet: both metadata-track sites shelled out to the
# bare name ``alembic``. The console script exists (the ``server`` extra installs
# it) but a subprocess only finds it when the venv's ``bin/`` is on PATH — not
# true under ``uv run``, a systemd unit, or any wrapper invoking the interpreter
# by absolute path. There ``subprocess.run`` raised a bare
# ``FileNotFoundError: 'alembic'``, the Postgres metadata schema was never
# created, and the server then started against a schemaless database and
# accepted 41,774 uploads with HTTP 201 and no metadata rows.


def test_alembic_argv_runs_through_this_interpreter() -> None:
    """The argv must name sys.executable, never the bare `alembic` console script."""
    import sys

    from novafabric.metadata_store.cli import _alembic_argv

    argv = _alembic_argv("/tmp/alembic.ini", "head")
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "alembic"]
    assert argv[0] != "alembic" and "alembic" not in argv[:1]
    assert argv[-3:] == ["/tmp/alembic.ini", "upgrade", "head"]


def test_upgrade_survives_an_empty_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """With PATH emptied, the upgrade still resolves alembic and succeeds.

    This is the exact deployment shape that produced B13: the interpreter is
    reachable, the console script is not.
    """
    monkeypatch.setenv("PATH", "")
    ok, message = run_alembic_upgrade("sqlite", revision="head")
    assert ok, message
    assert "FileNotFoundError" not in message


def test_missing_alembic_yields_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If alembic is genuinely absent, the caller gets a message, not a traceback."""
    import subprocess

    from novafabric.metadata_store import cli as md_cli

    def _boom(*_a: object, **_kw: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "alembic")

    monkeypatch.setattr(subprocess, "run", _boom)
    ok, message = run_alembic_upgrade("sqlite", revision="head")
    assert ok is False
    assert "not installed" in message
    assert "novafabric[server]" in message
    assert message == md_cli._ALEMBIC_MISSING_HINT


def test_upgrade_command_exits_2_when_alembic_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Typer command reports the same thing instead of crashing."""
    import subprocess

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "metadata.db"))

    def _boom(*_a: object, **_kw: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "alembic")

    monkeypatch.setattr(subprocess, "run", _boom)
    result = runner.invoke(app, ["db", "upgrade", "--backend", "sqlite"])
    assert result.exit_code == 2, result.output
    assert "not installed" in result.output
