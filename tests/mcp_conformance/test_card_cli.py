"""NF-039 R8 — `nova mcp card show|validate`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.mcp.servercard import build_server_card

runner = CliRunner()


def test_card_show_json_is_a_valid_card() -> None:
    result = runner.invoke(app, ["mcp", "card", "show", "--json"])
    assert result.exit_code == 0, result.output
    from novafabric.mcp.servercard import validate_server_card

    validate_server_card(json.loads(result.output))


def test_card_show_human_output_names_the_auth_in_force() -> None:
    """An operator reading this must see what auth clients will face."""
    result = runner.invoke(app, ["mcp", "card", "show"])
    assert result.exit_code == 0, result.output
    assert "protocolVersion: 2026-07-28" in result.output
    assert "auth:" in result.output


def test_card_show_respects_base_url() -> None:
    result = runner.invoke(
        app, ["mcp", "card", "show", "--json", "--base-url", "https://nova.example"]
    )
    assert json.loads(result.output)["endpoints"][0]["url"] == "https://nova.example/mcp"


def test_card_validate_accepts_a_generated_card(tmp_path: Path) -> None:
    path = tmp_path / "card.json"
    path.write_text(json.dumps(build_server_card().model_dump(mode="json", exclude_none=True)))
    result = runner.invoke(app, ["mcp", "card", "validate", str(path)])
    assert result.exit_code == 0, result.output
    assert "valid SEP-1649" in result.output


def test_card_validate_rejects_an_incomplete_card(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "x"}))
    result = runner.invoke(app, ["mcp", "card", "validate", str(path)])
    assert result.exit_code == 1
    assert "invalid Server Card" in result.output


def test_card_validate_missing_file_is_exit_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mcp", "card", "validate", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_card_validate_malformed_json_is_exit_2(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    result = runner.invoke(app, ["mcp", "card", "validate", str(path)])
    assert result.exit_code == 2
