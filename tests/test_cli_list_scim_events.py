"""ADR-0139 D3 CLI — `nova server list-scim-events` (backlog A6 CLI surface).

Read-only view over the append-only SCIM provisioning audit trail (auditor use):
the whole trail, filterable by subject, with an optional JSON view for tooling.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.server import scim_store

runner = CliRunner()


def _seed(db: Path) -> None:
    scim_store.append_audit_event(
        actor="scim-token:abc", operation="user.create", resource_type="User",
        subject="alice@example.com", roles_before=[], roles_after=[], db_path=db,
    )
    scim_store.append_audit_event(
        actor="scim-token:abc", operation="group-role-remap", resource_type="Group",
        subject="bob@example.com", roles_before=[], roles_after=["writer"], db_path=db,
    )


def test_list_scim_events_shows_the_audit_trail(tmp_path: Path) -> None:
    db = tmp_path / "scim.db"
    _seed(db)
    result = runner.invoke(app, ["server", "list-scim-events", "--db-path", str(db)])
    assert result.exit_code == 0, result.output
    assert "alice@example.com" in result.output
    assert "bob@example.com" in result.output
    assert "group-role-remap" in result.output


def test_list_scim_events_filters_by_subject(tmp_path: Path) -> None:
    db = tmp_path / "scim.db"
    _seed(db)
    result = runner.invoke(
        app,
        ["server", "list-scim-events", "--subject", "bob@example.com", "--db-path", str(db)],
    )
    assert result.exit_code == 0, result.output
    assert "bob@example.com" in result.output
    assert "alice@example.com" not in result.output


def test_list_scim_events_json_output_is_machine_readable(tmp_path: Path) -> None:
    db = tmp_path / "scim.db"
    _seed(db)
    result = runner.invoke(
        app, ["server", "list-scim-events", "--json", "--db-path", str(db)]
    )
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)
    assert len(events) == 2
    assert {e["subject"] for e in events} == {"alice@example.com", "bob@example.com"}
    # roles_after is decoded back to a list, not a JSON string
    remap = next(e for e in events if e["operation"] == "group-role-remap")
    assert remap["roles_after"] == ["writer"]


def test_list_scim_events_empty_trail_is_not_an_error(tmp_path: Path) -> None:
    db = tmp_path / "scim.db"
    result = runner.invoke(app, ["server", "list-scim-events", "--db-path", str(db)])
    assert result.exit_code == 0, result.output
