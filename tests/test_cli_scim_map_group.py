"""ADR-0139 D3 CLI — `nova server scim-map-group` (backlog A6 write-side CLI).

Declares an IdP-group → RBAC-role mapping in the server config (ADR-0029), the
operator counterpart to the /scim/v2/Groups routes. Validated against the six roles.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.server.config import load_config

runner = CliRunner()


def test_map_group_writes_mapping_to_config(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    result = runner.invoke(
        app, ["server", "scim-map-group", "Engineering", "writer", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert load_config(cfg).scim.group_role_map == {"Engineering": "writer"}


def test_map_group_is_additive_across_invocations(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    runner.invoke(app, ["server", "scim-map-group", "Engineering", "writer", "--config", str(cfg)])
    runner.invoke(app, ["server", "scim-map-group", "SRE-Admins", "admin", "--config", str(cfg)])
    assert load_config(cfg).scim.group_role_map == {
        "Engineering": "writer",
        "SRE-Admins": "admin",
    }


def test_map_group_rejects_unknown_role(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    result = runner.invoke(
        app, ["server", "scim-map-group", "Engineering", "superuser", "--config", str(cfg)]
    )
    assert result.exit_code == 1
    assert not cfg.exists()  # nothing written on a rejected mapping


def test_map_group_remove_deletes_the_mapping(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    runner.invoke(app, ["server", "scim-map-group", "Engineering", "writer", "--config", str(cfg)])
    result = runner.invoke(
        app, ["server", "scim-map-group", "Engineering", "--remove", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert load_config(cfg).scim.group_role_map == {}


def test_list_empty_map(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    result = runner.invoke(app, ["server", "scim-map-group", "--list", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "No group→role mappings configured." in result.output


def test_list_shows_effective_map(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    runner.invoke(app, ["server", "scim-map-group", "Engineering", "writer", "--config", str(cfg)])
    runner.invoke(app, ["server", "scim-map-group", "SRE-Admins", "admin", "--config", str(cfg)])
    result = runner.invoke(app, ["server", "scim-map-group", "--list", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Engineering" in result.output and "writer" in result.output
    assert "SRE-Admins" in result.output and "admin" in result.output
    # --list is read-only: the config file is unchanged by listing.
    assert load_config(cfg).scim.group_role_map == {
        "Engineering": "writer",
        "SRE-Admins": "admin",
    }


def test_group_required_without_list(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    result = runner.invoke(app, ["server", "scim-map-group", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "group displayName is required" in result.output


def test_role_required_without_remove_or_list(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    result = runner.invoke(app, ["server", "scim-map-group", "Engineering", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "role is required" in result.output


def test_map_group_preserves_other_config_fields(tmp_path: Path) -> None:
    cfg = tmp_path / "server.yaml"
    cfg.write_text(yaml.safe_dump({"host": "0.0.0.0", "port": 9999}))
    runner.invoke(app, ["server", "scim-map-group", "Engineering", "writer", "--config", str(cfg)])
    loaded = load_config(cfg)
    assert loaded.host == "0.0.0.0"
    assert loaded.port == 9999
    assert loaded.scim.group_role_map == {"Engineering": "writer"}
