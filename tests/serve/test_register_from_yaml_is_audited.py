"""``POST /api/assets/register-from-yaml`` leaves an audit record (review serve-08).

The suggest-register panel's route registered assets with no dashboard-audit entry,
unlike ``POST /api/assets``: a registration made from the dashboard was invisible
in ``/api/audit``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "register-audit-token"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    store = tmp_path / "runs"
    store.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "reg.db"))


def _entries(client: TestClient) -> list[dict[str, object]]:
    resp = client.get("/api/audit", params={"action": "register_asset"}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


def test_a_successful_registration_is_audited(client: TestClient, fixtures_dir: Path) -> None:
    spec = (fixtures_dir / "valid_model.yaml").read_text()
    resp = client.post("/api/assets/register-from-yaml", headers=HEADERS, json={"spec_yaml": spec})
    assert resp.json()["ok"] is True, resp.json()
    (entry,) = _entries(client)
    assert entry["args"]["name"] == "fraud-model"
    assert entry["result"] == "ok"


def test_a_refused_registration_is_audited_too(client: TestClient) -> None:
    resp = client.post("/api/assets/register-from-yaml", headers=HEADERS,
                       json={"spec_yaml": "not: a valid spec\n"})
    assert resp.json()["ok"] is False
    (entry,) = _entries(client)
    assert entry["result"] == "error"
