"""Tests for the v0.55 dashboard CLI-coverage expansion routes.

Covers the new serve endpoints that surface previously CLI-only commands:
  - incidents (open/list/status/transition/export) — Art. 73 deadline clock
  - diagnose, evidence completeness/bind, seal ratchet, daemon status,
    export-system-card, rebuild-metadata-db (gated).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-cli-coverage"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Isolate incident/ratchet/key state under a temp NOVAFABRIC_HOME.
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova-home"))
    app = create_app(
        capsule_dir=tmp_path / "runs",
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


class TestIncidents:
    def test_open_list_get_transition_export(self, client: TestClient) -> None:
        # Open
        r = client.post(
            f"/api/incidents?{TOKEN_Q}",
            headers=H,
            json={
                "title": "Unexpected tool use",
                "classification": "unauthorized_tool_use",
                "severity": "high",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        inc = body["incident"]
        incident_id = inc["id"]
        # The Art.73 clock must be computed.
        assert "deadlines" in inc and len(inc["deadlines"]) >= 1
        assert inc["status"] == "open"

        # List
        r = client.get(f"/api/incidents?{TOKEN_Q}", headers=H)
        assert r.status_code == 200
        assert any(i["id"] == incident_id for i in r.json()["incidents"])

        # Get one
        r = client.get(f"/api/incidents/{incident_id}?{TOKEN_Q}", headers=H)
        assert r.json()["incident"]["id"] == incident_id

        # Transition open -> reported
        r = client.post(
            f"/api/incidents/{incident_id}/transition?{TOKEN_Q}",
            headers=H,
            json={"to_status": "reported"},
        )
        assert r.json()["incident"]["status"] == "reported"

        # Export (AIM + NIS2)
        r = client.get(f"/api/incidents/{incident_id}/export?{TOKEN_Q}&fmt=aim", headers=H)
        assert r.json()["ok"] is True
        assert r.json()["report"]["report_type"] == "oecd-aim"
        r = client.get(f"/api/incidents/{incident_id}/export?{TOKEN_Q}&fmt=nis2", headers=H)
        assert r.json()["ok"] is True

    def test_open_requires_title_and_classification(self, client: TestClient) -> None:
        r = client.post(f"/api/incidents?{TOKEN_Q}", headers=H, json={"title": "x"})
        assert r.status_code == 422


class TestRatchet:
    def test_init_then_status(self, client: TestClient) -> None:
        r = client.post(
            f"/api/seal/ratchet/init?{TOKEN_Q}", headers=H, json={"node_id": "node-a"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["state"]["epoch"] == 0

        r = client.get(f"/api/seal/ratchet/status?{TOKEN_Q}&node_id=node-a", headers=H)
        assert r.json()["ok"] is True
        assert r.json()["state"]["node_id"] == "node-a"

    def test_init_requires_node_id(self, client: TestClient) -> None:
        r = client.post(f"/api/seal/ratchet/init?{TOKEN_Q}", headers=H, json={})
        assert r.status_code == 422

    def test_rotate_requires_confirmation(self, client: TestClient) -> None:
        # api.ratchetRotate always sends confirmed=true; a bare call without it 400s.
        r = client.post(
            f"/api/seal/ratchet/rotate?{TOKEN_Q}",
            headers=H,
            json={"node_id": "node-a"},
        )
        assert r.status_code == 400


class TestDaemonStatus:
    def test_status_read_only(self, client: TestClient) -> None:
        r = client.get(f"/api/ops/daemon-status?{TOKEN_Q}", headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["alive"] is False  # no daemon running in tests


class TestDiagnoseAndEvidence:
    def test_diagnose_missing_capsule_degrades(self, client: TestClient) -> None:
        r = client.get(f"/api/runs/nope/diagnose?{TOKEN_Q}", headers=H)
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_completeness_missing_capsule_degrades(self, client: TestClient) -> None:
        r = client.get(f"/api/evidence/completeness/nope?{TOKEN_Q}", headers=H)
        assert r.status_code == 200
        assert r.json()["ok"] is False


class TestRebuildGate:
    def test_rebuild_requires_confirmation(self, client: TestClient) -> None:
        r = client.post(
            f"/api/admin/rebuild-metadata-db?{TOKEN_Q}", headers=H, json={}
        )
        assert r.status_code == 400


class TestAuthRequired:
    def test_incidents_list_requires_token(self, client: TestClient) -> None:
        r = client.get("/api/incidents", headers=H)
        assert r.status_code in (401, 403)
