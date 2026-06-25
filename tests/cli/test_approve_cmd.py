from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _register_and_stage(db_path: Path, name: str = "model-x", version: str = "1.0.0") -> None:
    from novafabric.registry.service import promote_asset, register_asset

    register_asset(name, version, "agent", db_path=db_path)
    promote_asset(name, version, "staging", db_path=db_path)
    promote_asset(name, version, "pending_approval", db_path=db_path)


def test_approve_missing_version_exits_1() -> None:
    result = runner.invoke(app, ["approve", "no-version-here"])
    assert result.exit_code == 1
    assert "version" in result.output.lower() or "version" in (result.output + "").lower()


def test_approve_asset_not_found_exits_1(tmp_path: Path) -> None:
    with patch("novafabric.cli.approve.approve_asset", side_effect=__import__("novafabric.registry.service", fromlist=["AssetNotFoundError"]).AssetNotFoundError("ghost@1.0.0 not found")):
        result = runner.invoke(app, ["approve", "ghost@1.0.0"])
    assert result.exit_code == 1


def test_approve_invalid_transition_exits_1(tmp_path: Path) -> None:
    from novafabric.registry.service import InvalidLifecycleTransitionError

    with patch(
        "novafabric.cli.approve.approve_asset",
        side_effect=InvalidLifecycleTransitionError("not in pending_approval"),
    ):
        result = runner.invoke(app, ["approve", "model@1.0.0"])
    assert result.exit_code == 1


def test_approve_success() -> None:
    with patch(
        "novafabric.cli.approve.approve_asset",
        return_value={"approver": "alice", "status": "production"},
    ):
        result = runner.invoke(app, ["approve", "model-x@1.0.0", "--approver", "alice"])
    assert result.exit_code == 0
    assert "alice" in result.output


def test_approve_help() -> None:
    result = runner.invoke(app, ["approve", "--help"])
    assert result.exit_code == 0
    assert "sign-off" in result.output.lower() or "approval" in result.output.lower()
