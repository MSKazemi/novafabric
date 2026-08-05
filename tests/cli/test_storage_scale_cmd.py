from __future__ import annotations

from unittest.mock import MagicMock, patch

from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_NOVA_OBJECT_STORE = "novafabric.storage.nova_object_store.NovaObjectStore"


# ---------------------------------------------------------------------------
# storage inspect
# ---------------------------------------------------------------------------


def test_storage_inspect_shows_run_id() -> None:
    result = runner.invoke(app, ["storage", "inspect", "--run-id", "run-abc123"])
    assert result.exit_code == 0
    assert "run-abc123" in result.output
    assert "audit" in result.output


def test_storage_inspect_mentions_pii() -> None:
    result = runner.invoke(app, ["storage", "inspect", "--run-id", "run-xyz"])
    assert "pii" in result.output.lower()


def test_storage_inspect_help() -> None:
    result = runner.invoke(app, ["storage", "inspect", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--run-id")


# ---------------------------------------------------------------------------
# storage validate
# ---------------------------------------------------------------------------


def test_storage_validate_success() -> None:
    mock_store = MagicMock()
    mock_store.validate.return_value = {"object_lock": True}
    with patch(_NOVA_OBJECT_STORE, return_value=mock_store):
        result = runner.invoke(
            app, ["storage", "validate", "--endpoint", "http://localhost:9000"]
        )
    assert result.exit_code == 0
    assert "validated" in result.output


def test_storage_validate_object_lock_not_supported_exits_1() -> None:
    from novafabric.storage.nova_object_store import ObjectLockNotSupportedError

    mock_store = MagicMock()
    mock_store.validate.side_effect = ObjectLockNotSupportedError("no lock")
    with patch(_NOVA_OBJECT_STORE, return_value=mock_store):
        result = runner.invoke(app, ["storage", "validate"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_storage_validate_generic_error_exits_1() -> None:
    mock_store = MagicMock()
    mock_store.validate.side_effect = ConnectionError("refused")
    with patch(_NOVA_OBJECT_STORE, return_value=mock_store):
        result = runner.invoke(app, ["storage", "validate"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_storage_validate_help() -> None:
    result = runner.invoke(app, ["storage", "validate", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--endpoint") or "--bucket" in result.output
