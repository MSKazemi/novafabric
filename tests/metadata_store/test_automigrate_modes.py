"""Tests for automigrate modes and CLI help for the metadata store.

FR-06: nova db upgrade; automigrate flag; bootstrap behaviour.
"""
from __future__ import annotations

import warnings

import pytest

# ---------------------------------------------------------------------------
# Test 1: automigrate=false is a no-op (bootstrap still succeeds)
# ---------------------------------------------------------------------------


def test_automigrate_false_noop(tmp_path, monkeypatch) -> None:
    """With NOVAFABRIC_DB_AUTOMIGRATE=false, bootstrap() still works fine."""
    monkeypatch.setenv("NOVAFABRIC_DB_AUTOMIGRATE", "false")
    monkeypatch.setenv("NOVAFABRIC_METADATA_BACKEND", "sqlite")
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "meta.db"))

    from novafabric.metadata_store.factory import get_metadata_store

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        store = get_metadata_store()
        store.bootstrap()
        health = store.health_check()

    assert health["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 2: migrate-to-postgres --help exits 0 and documents --source/--target
# ---------------------------------------------------------------------------


def test_cli_migrate_to_postgres_help() -> None:
    """``nova metadata-migrate-to-postgres --help`` exits 0 and shows --source, --target."""
    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    runner = CliRunner()
    result = runner.invoke(metadata_db_app, ["migrate-to-postgres", "--help"])

    assert result.exit_code == 0, f"--help exited non-zero:\n{result.output}"
    assert "--source" in result.output, "Missing --source in help output"
    assert "--target" in result.output, "Missing --target in help output"


# ---------------------------------------------------------------------------
# Test 3: db upgrade --help exits 0
# ---------------------------------------------------------------------------


def test_cli_db_upgrade_help() -> None:
    """``nova db upgrade --help`` exits 0."""
    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    runner = CliRunner()
    result = runner.invoke(metadata_db_app, ["upgrade", "--help"])

    assert result.exit_code == 0, f"--help exited non-zero:\n{result.output}"


# ---------------------------------------------------------------------------
# Test 4: factory raises RuntimeError when backend=postgres and DSN is missing
# ---------------------------------------------------------------------------


def test_factory_postgres_missing_dsn_raises(monkeypatch) -> None:
    """get_metadata_store() raises RuntimeError if NOVAFABRIC_METADATA_BACKEND=postgres
    and NOVAFABRIC_METADATA_DSN is not set (covers factory.py lines 30-35)."""
    monkeypatch.setenv("NOVAFABRIC_METADATA_BACKEND", "postgres")
    monkeypatch.delenv("NOVAFABRIC_METADATA_DSN", raising=False)

    # Force re-import so the env var is re-read (factory uses os.environ at call time)
    from novafabric.metadata_store.factory import get_metadata_store

    with pytest.raises(RuntimeError, match="NOVAFABRIC_METADATA_DSN"):
        get_metadata_store()


# ---------------------------------------------------------------------------
# Test 5: factory returns PostgresMetadataStore when DSN is provided
# ---------------------------------------------------------------------------


def test_factory_postgres_with_dsn_returns_postgres_store(monkeypatch) -> None:
    """get_metadata_store() returns a PostgresMetadataStore when a DSN is provided
    (covers factory.py lines 36-38). Does not connect — just checks the return type."""
    monkeypatch.setenv("NOVAFABRIC_METADATA_BACKEND", "postgres")
    monkeypatch.setenv("NOVAFABRIC_METADATA_DSN", "postgresql://nova:nova@localhost:5432/nova_test")

    from novafabric.metadata_store.factory import get_metadata_store
    from novafabric.metadata_store.postgres import PostgresMetadataStore

    store = get_metadata_store()
    assert isinstance(store, PostgresMetadataStore)


# ---------------------------------------------------------------------------
# Test 6: migrate-to-postgres exits 2 when source file does not exist
# ---------------------------------------------------------------------------


def test_cli_migrate_source_not_found(tmp_path) -> None:
    """migrate-to-postgres exits code 2 when --source path does not exist."""
    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    runner = CliRunner()
    result = runner.invoke(
        metadata_db_app,
        [
            "migrate-to-postgres",
            "--source", str(tmp_path / "nonexistent.db"),
            "--target", "postgresql://nova:nova@localhost:5432/nova",
        ],
    )

    assert result.exit_code == 2, f"Expected exit 2, got {result.exit_code}:\n{result.output}"
    assert "not found" in result.output.lower() or "source" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 7: upgrade exits 2 for an unknown backend value
# ---------------------------------------------------------------------------


def test_cli_upgrade_invalid_backend() -> None:
    """``nova db upgrade --backend bogus`` exits code 2."""
    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    runner = CliRunner()
    result = runner.invoke(metadata_db_app, ["upgrade", "--backend", "bogus"])

    assert result.exit_code == 2, f"Expected exit 2, got {result.exit_code}:\n{result.output}"
    assert "bogus" in result.output


# ---------------------------------------------------------------------------
# Test 8: upgrade --backend postgres exits 2 when NOVAFABRIC_METADATA_DSN is unset
# ---------------------------------------------------------------------------


def test_cli_upgrade_postgres_missing_dsn(monkeypatch) -> None:
    """``nova db upgrade --backend postgres`` exits 2 when DSN env var is absent."""
    monkeypatch.delenv("NOVAFABRIC_METADATA_DSN", raising=False)

    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    runner = CliRunner()
    result = runner.invoke(metadata_db_app, ["upgrade", "--backend", "postgres"])

    assert result.exit_code == 2, f"Expected exit 2, got {result.exit_code}:\n{result.output}"
    assert "NOVAFABRIC_METADATA_DSN" in result.output


# ---------------------------------------------------------------------------
# Test 9: _redact_dsn helper covers both the password-redaction path and fallback
# ---------------------------------------------------------------------------


def test_redact_dsn_with_password() -> None:
    """_redact_dsn replaces the password with *** in a valid DSN."""
    from novafabric.metadata_store.cli import _redact_dsn

    result = _redact_dsn("postgresql://user:secret@host:5432/db")
    assert "secret" not in result
    assert "***" in result
    assert "user" in result


def test_redact_dsn_fallback_no_url() -> None:
    """_redact_dsn returns the original string when it is not a URL."""
    from novafabric.metadata_store.cli import _redact_dsn

    raw = "not-a-real-dsn"
    assert _redact_dsn(raw) == raw
