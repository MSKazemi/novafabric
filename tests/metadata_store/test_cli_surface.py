"""Operator-facing surface of `nova db …`, without a live database.

The MetadataStore Security Gate enforces 85% coverage on
`src/novafabric/metadata_store/`. It has failed on **every run in its recorded
history** — first on mypy, then on the tests #23 broke, and now on this
threshold: `cli.py` sits at 66% because its command wrappers, error paths and
DSN redaction were never exercised.

These are the parts an operator actually meets — `nova db upgrade`,
`nova db migrate-to-postgres`, the messages they print when something is wrong —
so testing them is worth doing on its own merits, not merely to move a number.
`alembic` is invoked through `subprocess`, so it is stubbed: the behaviour under
test is which arguments and environment the command builds, and how it reacts to
a non-zero exit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.metadata_store import cli as mcli

runner = CliRunner()


# ---------------------------------------------------------------------------
# _redact_dsn — a password must never reach a console or a log line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://user:hunter2@host:5432/db", "postgresql://user:***@host:5432/db"),
        # No password to redact.
        ("postgresql://user@host/db", "postgresql://user@host/db"),
        # Not a URL at all — a libpq keyword string passes through untouched.
        ("host=localhost dbname=nova", "host=localhost dbname=nova"),
        ("", ""),
    ],
)
def test_redact_dsn(dsn: str, expected: str) -> None:
    assert mcli._redact_dsn(dsn) == expected


def test_redact_dsn_never_leaks_the_password() -> None:
    """The property that matters, stated independently of the exact format."""
    assert "hunter2" not in mcli._redact_dsn("postgresql://u:hunter2@h/db")


# ---------------------------------------------------------------------------
# _alembic_ini_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_alembic_ini_path_resolves_for_both_backends(backend: str) -> None:
    path = mcli._alembic_ini_path(backend)

    assert path is not None, f"no packaged alembic.ini for {backend}"
    assert path.is_file()


def test_alembic_ini_path_returns_none_for_an_unknown_backend() -> None:
    assert mcli._alembic_ini_path("mysql") is None


# ---------------------------------------------------------------------------
# run_alembic_upgrade — argument and environment construction
# ---------------------------------------------------------------------------


def test_run_alembic_upgrade_rejects_an_unknown_backend() -> None:
    ok, detail = mcli.run_alembic_upgrade("mysql")

    assert ok is False
    assert "mysql" in detail


def test_run_alembic_upgrade_requires_a_dsn_for_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVAFABRIC_METADATA_DSN", raising=False)

    ok, detail = mcli.run_alembic_upgrade("postgres")

    assert ok is False
    assert "DSN" in detail


def test_run_alembic_upgrade_passes_the_dsn_by_environment_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSN carries a password; it must reach the child via env, not argv.

    Anything in argv is visible in `ps` to every user on the host.
    """
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    dsn = "postgresql://user:hunter2@host/db"
    ok, _ = mcli.run_alembic_upgrade("postgres", dsn=dsn)

    assert ok is True
    assert "hunter2" not in " ".join(seen["cmd"])
    assert seen["env"]["NOVAFABRIC_METADATA_DSN"] == dsn


def test_run_alembic_upgrade_reports_a_child_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, detail = mcli.run_alembic_upgrade("sqlite")

    assert ok is False
    assert detail


def test_run_alembic_upgrade_scrubs_the_dsn_out_of_surfaced_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing child often echoes its connection string. It must not survive."""
    dsn = "postgresql://user:hunter2@host/db"

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout=f"could not connect to {dsn}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, detail = mcli.run_alembic_upgrade("postgres", dsn=dsn)

    assert ok is False
    assert "hunter2" not in detail


# ---------------------------------------------------------------------------
# The Typer commands
# ---------------------------------------------------------------------------


def test_db_upgrade_help_lists_its_flags() -> None:
    result = runner.invoke(mcli.metadata_db_app, ["upgrade", "--help"])

    assert result.exit_code == 0
    assert_flag_in_help(result, "--backend")


def test_db_upgrade_exits_non_zero_when_alembic_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.invoke(mcli.metadata_db_app, ["upgrade", "--backend", "sqlite"])

    assert result.exit_code == 3, "the child's exit code must reach the caller"


def test_db_upgrade_succeeds_when_alembic_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "meta.db"))

    result = runner.invoke(mcli.metadata_db_app, ["upgrade", "--backend", "sqlite"])

    assert result.exit_code == 0
    # Since B13 the argv runs alembic through *this* interpreter rather than the
    # bare console script, which a subprocess only finds when the venv's bin/ is
    # on PATH. The track is still asserted, via the ini path.
    assert calls
    assert calls[0][0] == sys.executable
    assert calls[0][1:3] == ["-m", "alembic"]
    assert "upgrade" in calls[0]
    assert "metadata_store" in str(calls[0][calls[0].index("-c") + 1])


def test_migrate_to_postgres_help_lists_its_flags() -> None:
    result = runner.invoke(mcli.metadata_db_app, ["migrate-to-postgres", "--help"])

    assert result.exit_code == 0
    assert_flag_in_help(result, "--source")
    assert_flag_in_help(result, "--target")


def test_migrate_to_postgres_rejects_a_missing_source(tmp_path: Path) -> None:
    result = runner.invoke(
        mcli.metadata_db_app,
        [
            "migrate-to-postgres",
            "--source",
            str(tmp_path / "does-not-exist.db"),
            "--target",
            "postgresql://u:p@h/db",
        ],
    )

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# _print_summary_table — rendering must not raise on either row shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pg_rows", "label"),
    [(3, "matching counts"), (2, "a short Postgres count -> mismatch")],
)
def test_print_summary_table_renders_every_row_shape(pg_rows: int, label: str) -> None:
    """Skipped, matching and mismatched rows must all render.

    The mismatch branch is the one that tells an operator their migration lost
    rows, so it is the branch that must not be the one that raises.
    """
    mcli._print_summary_table(
        {
            "runs": {"sqlite_rows": 3, "written": 3, "pg_rows": pg_rows, "skipped": 0},
            "capsules": {"sqlite_rows": 0, "written": 0, "pg_rows": 0, "skipped": 1},
        }
    )


# ---------------------------------------------------------------------------
# `--track registry` — the second migration track
# ---------------------------------------------------------------------------


def test_registry_track_against_postgres_requires_a_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 2, not a stack trace, when the operator forgot the env var."""
    monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)

    result = runner.invoke(
        mcli.metadata_db_app, ["upgrade", "--backend", "postgres", "--track", "registry"]
    )

    assert result.exit_code == 2
    assert "NOVAFABRIC_POSTGRES_DSN" in result.output


def test_registry_track_reports_a_failure_without_echoing_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration failure must not print the connection string it failed on.

    The blanket `except Exception` here exists for exactly this: alembic and the
    driver both like to include the DSN in their messages.
    """
    dsn = "postgresql://user:hunter2@host/db"
    monkeypatch.setenv("NOVAFABRIC_POSTGRES_DSN", dsn)

    from novafabric.migrations import registry_track

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"connection to {dsn} failed")

    monkeypatch.setattr(registry_track, "registry_alembic_config", boom)

    result = runner.invoke(
        mcli.metadata_db_app, ["upgrade", "--backend", "postgres", "--track", "registry"]
    )

    assert result.exit_code == 1
    assert "hunter2" not in result.output


def test_registry_track_succeeds_for_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    from alembic import command as alembic_command

    from novafabric.migrations import registry_track

    monkeypatch.setattr(registry_track, "registry_alembic_config", lambda *a, **k: object())
    monkeypatch.setattr(alembic_command, "upgrade", lambda *a, **k: None)

    result = runner.invoke(
        mcli.metadata_db_app, ["upgrade", "--backend", "sqlite", "--track", "registry"]
    )

    assert result.exit_code == 0
