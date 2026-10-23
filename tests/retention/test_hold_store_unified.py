"""One legal-hold store for every writer and every deletion reader.

Before this fix ``nova hold`` wrote ``./.novafabric/registries/<r>/holds.jsonl``
(relative to the shell's working directory) while ``POST /api/holds`` wrote
``<capsule_dir>/../registries/<r>/holds.jsonl``. ``nova retention`` read only the
first, the governed server delete paths only the second. A hold placed through the
dashboard/API therefore did not stop ``nova retention apply`` from purging the held
capsules, and a hold placed with the CLI did not stop the server's delete routes.

These tests drive the real surfaces (FastAPI ``TestClient`` + Typer ``CliRunner``)
in the default layout: ``NOVAFABRIC_HOME`` for data, an unrelated working directory
for the shell.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from novafabric.cli.main import app as cli_app
from novafabric.serve.app import create_app
from novafabric.server.capsule_delete import DeleteBlockedError, check_deletable

runner = CliRunner()

REGISTRY = "prod"
TOKEN = "hold-store-token"
HEADERS = {"host": "127.0.0.1:4321", "content-type": "application/json"}
OLD = datetime.now(tz=timezone.utc) - timedelta(days=400)

_POLICY = """
version: 1
registry: prod
retention_days: 30
deletion_mode: defensible
bindings:
  - id: purge-old
    match:
      tag: trade
    window: P90D
    action: purge
"""


class Workspace:
    def __init__(self, tmp_path: Path) -> None:
        self.cwd = tmp_path / "project"
        self.home = tmp_path / "home"
        self.capsules = self.home / "capsules"

    def capsule(self, run_id: str = "cap-old") -> Path:
        d = self.capsules / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "capsule.json").write_text(
            json.dumps(
                {"run_id": run_id, "created_at": OLD.isoformat(), "metadata": {"tags": "trade"}}
            )
        )
        (d / "capsule.yaml").write_text(f"run_id: {run_id}\n")
        return d

    def api(self, capsule_dir: Path | None = None) -> TestClient:
        return TestClient(
            create_app(token=TOKEN, capsule_dir=capsule_dir or self.capsules, db_path=None)
        )


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    w = Workspace(tmp_path)
    w.cwd.mkdir()
    monkeypatch.chdir(w.cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(w.home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    policy_dir = w.cwd / ".novafabric" / "registries" / REGISTRY
    policy_dir.mkdir(parents=True)
    (policy_dir / "retention-policy.yaml").write_text(_POLICY)
    return w


def _api_place(tc: TestClient, registry: str = REGISTRY) -> str:
    r = tc.post(
        "/api/holds", params={"token": TOKEN}, headers=HEADERS,
        json={"registry": registry, "reason": "SEC examination"},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["hold_id"])


def _cli_place(registry: str = REGISTRY) -> str:
    r = runner.invoke(cli_app, ["hold", "create", registry, "--reason", "litigation"])
    assert r.exit_code == 0, r.output
    return next(w for w in r.output.split() if w.startswith("hold-"))


def _apply() -> dict[str, Any]:
    r = runner.invoke(
        cli_app, ["retention", "apply", "--registry", REGISTRY, "--yes", "--json"],
        catch_exceptions=False,
    )
    assert r.exit_code == 0, r.output
    return dict(json.loads(r.output))


# ---------------------------------------------------------------------------
# The reported defect: API hold invisible to the CLI sweep
# ---------------------------------------------------------------------------


def test_api_placed_hold_stops_cli_retention_purge(ws: Workspace) -> None:
    cap = ws.capsule()
    _api_place(ws.api())
    payload = _apply()
    assert cap.is_dir(), "nova retention purged a capsule under an API-placed legal hold"
    assert [h["item_id"] for h in payload["held"]] == ["cap-old"]


def test_api_placed_hold_shows_in_cli_explain_and_list(ws: Workspace) -> None:
    ws.capsule()
    hold_id = _api_place(ws.api())
    listed = runner.invoke(cli_app, ["hold", "list", REGISTRY])
    assert hold_id in listed.output
    explained = runner.invoke(
        cli_app, ["retention", "explain", "cap-old", "--registry", REGISTRY, "--json"]
    )
    assert explained.exit_code == 0, explained.output
    assert json.loads(explained.output)["active_legal_holds"] == [hold_id]


def test_api_placed_hold_blocks_cli_capsule_delete(ws: Workspace) -> None:
    _api_place(ws.api())
    r = runner.invoke(cli_app, ["capsule", "delete", "cap-x", "--registry", REGISTRY])
    assert r.exit_code == 1
    assert "legal hold" in r.output


def test_api_placed_hold_reported_by_export_retention(ws: Workspace) -> None:
    from novafabric.compliance.export.finance.retention_collect import (
        read_registry_holds,
    )

    hold_id = _api_place(ws.api())
    facts = read_registry_holds(REGISTRY)
    assert facts.present
    assert facts.active_hold_ids == [hold_id]


# ---------------------------------------------------------------------------
# The other direction: CLI hold invisible to the server delete path
# ---------------------------------------------------------------------------


def test_cli_placed_hold_blocks_server_delete(ws: Workspace) -> None:
    ws.capsule()
    hold_id = _cli_place()
    with pytest.raises(DeleteBlockedError) as exc_info:
        check_deletable(ws.capsules, "cap-old")
    assert exc_info.value.code == "legal_hold_active"
    assert hold_id in exc_info.value.details["hold_ids"]  # type: ignore[operator]


def test_cli_placed_hold_listed_by_api(ws: Workspace) -> None:
    hold_id = _cli_place()
    r = ws.api().get("/api/holds", params={"token": TOKEN}, headers=HEADERS)
    assert r.status_code == 200
    ids = [h["hold_id"] for reg in r.json()["registries"] for h in reg["holds"]]
    assert ids == [hold_id]
    assert r.json()["total_active"] == 1


def test_release_works_across_surfaces(ws: Workspace) -> None:
    cap = ws.capsule()
    tc = ws.api()
    api_hold = _api_place(tc)
    cli_hold = _cli_place()
    released = runner.invoke(cli_app, ["hold", "release", api_hold])
    assert released.exit_code == 0, released.output
    r = tc.post(f"/api/holds/{cli_hold}/release", params={"token": TOKEN}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert "No active holds" in runner.invoke(cli_app, ["hold", "list", REGISTRY]).output
    _apply()
    assert not cap.exists()  # no hold left: the due purge now goes ahead


# ---------------------------------------------------------------------------
# Migration: holds already written to either legacy location stay honoured
# ---------------------------------------------------------------------------


def _legacy_hold(root: Path, hold_id: str, registry: str = REGISTRY) -> None:
    path = root / registry / "holds.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "hold_id": hold_id, "registry": registry, "reason": "legacy",
        "duration_days": None, "created_at": OLD.isoformat(), "released_at": None,
    }
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def test_legacy_cwd_hold_still_blocks_sweep_and_server(ws: Workspace) -> None:
    cap = ws.capsule()
    _legacy_hold(ws.cwd / ".novafabric" / "registries", "hold-legacycli")
    assert _apply()["held"][0]["item_id"] == "cap-old"
    assert cap.is_dir()
    with pytest.raises(DeleteBlockedError):
        check_deletable(ws.capsules, "cap-old")


def test_legacy_serve_hold_beside_custom_capsule_dir_still_blocks_sweep(
    ws: Workspace, tmp_path: Path
) -> None:
    custom = tmp_path / "data" / "caps"
    cap = custom / "cap-old"
    cap.mkdir(parents=True)
    (cap / "capsule.json").write_text(
        json.dumps({"run_id": "cap-old", "created_at": OLD.isoformat(),
                    "metadata": {"tags": "trade"}})
    )
    _legacy_hold(custom.parent / "registries", "hold-legacyapi")
    r = runner.invoke(
        cli_app,
        ["retention", "apply", "--registry", REGISTRY, "--capsule-dir", str(custom),
         "--yes", "--json"],
        catch_exceptions=False,
    )
    assert r.exit_code == 0, r.output
    assert cap.is_dir()
    listed = ws.api(capsule_dir=custom).get(
        "/api/holds", params={"token": TOKEN}, headers=HEADERS
    )
    assert listed.json()["total_active"] == 1


def test_legacy_hold_can_be_released_from_either_surface(ws: Workspace) -> None:
    _legacy_hold(ws.cwd / ".novafabric" / "registries", "hold-oldcli")
    _legacy_hold(ws.home / "registries", "hold-oldapi")
    assert runner.invoke(cli_app, ["hold", "release", "hold-oldapi"]).exit_code == 0
    r = ws.api().post("/api/holds/hold-oldcli/release", params={"token": TOKEN},
                      headers=HEADERS)
    assert r.status_code == 200, r.text
    assert "No active holds" in runner.invoke(cli_app, ["hold", "list", REGISTRY]).output


# ---------------------------------------------------------------------------
# Failure and edge cases
# ---------------------------------------------------------------------------


def test_corrupt_ledger_line_fails_closed_for_the_sweep(ws: Workspace) -> None:
    cap = ws.capsule()
    path = ws.home / "registries" / REGISTRY / "holds.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{not json\n")
    _apply()
    assert cap.is_dir()


def test_cli_refuses_a_traversing_registry_name(ws: Workspace) -> None:
    r = runner.invoke(cli_app, ["hold", "create", "../escape", "--reason", "x"])
    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)
    assert not (ws.home / "escape").exists()
    assert not (ws.cwd / ".novafabric" / "escape").exists()


def test_hold_on_another_registry_does_not_hold_this_sweep(ws: Workspace) -> None:
    cap = ws.capsule()
    _api_place(ws.api(), registry="other")
    _apply()
    assert not cap.exists()
