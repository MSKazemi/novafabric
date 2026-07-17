"""Collector health-file trust — /tmp spoofing defense (v0.61 audit B4).

The collector-status endpoint used to read the world-writable
``/tmp/novafabric-collector-health.json`` as its FIRST candidate, so any
local user could plant that file and spoof collector health in the
dashboard. The trusted order is now: explicit env override, then the
user-owned ``~/.novafabric`` path, then the /tmp fallback ONLY when the
file is owned by the user running the server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import novafabric.serve.app as serve_app
from novafabric.serve.app import (
    _collector_health_paths,
    _owned_by_current_user,
    create_app,
)

TOKEN = "testtoken"
AUTH = {"token": TOKEN}
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "registry.db"
    )
    return TestClient(app)


def test_env_override_is_first_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_HEALTH_FILE", "/custom/health.json")
    paths = _collector_health_paths()
    assert paths[0] == Path("/custom/health.json")


def test_home_path_precedes_tmp_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_COLLECTOR_HEALTH_FILE", raising=False)
    paths = _collector_health_paths()
    home_idx = paths.index(Path.home() / ".novafabric" / "collector-health.json")
    tmp_idx = paths.index(Path(serve_app._DEFAULT_COLLECTOR_HEALTH))
    assert home_idx < tmp_idx


def test_owned_by_current_user_true_for_own_file(tmp_path: Path) -> None:
    f = tmp_path / "health.json"
    f.write_text("{}")
    assert _owned_by_current_user(f) is True


def test_owned_by_current_user_false_for_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "health.json"
    f.write_text("{}")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(f).st_uid + 1)
    assert _owned_by_current_user(f) is False


def test_spoofed_tmp_health_file_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A /tmp health file NOT owned by the server user must not be trusted."""
    spoof = tmp_path / "spoofed-health.json"
    spoof.write_text(json.dumps({"spool_lag": 0, "version": "evil"}))
    monkeypatch.setattr(serve_app, "_DEFAULT_COLLECTOR_HEALTH", str(spoof))
    monkeypatch.delenv("NOVA_COLLECTOR_HEALTH_FILE", raising=False)
    monkeypatch.setenv("NOVA_COLLECTOR_METRICS_URL", "http://127.0.0.1:19999/metrics")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(spoof).st_uid + 1)

    resp = client.get("/api/infra/collector", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["detected"] is False


def test_owned_tmp_health_file_is_still_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The /tmp fallback keeps working when the file belongs to the server user."""
    health = tmp_path / "own-health.json"
    health.write_text(json.dumps({"spool_lag": 7, "version": "0.2.0"}))
    monkeypatch.setattr(serve_app, "_DEFAULT_COLLECTOR_HEALTH", str(health))
    monkeypatch.delenv("NOVA_COLLECTOR_HEALTH_FILE", raising=False)

    resp = client.get("/api/infra/collector", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected"] is True
    assert data["spool_lag"] == 7
