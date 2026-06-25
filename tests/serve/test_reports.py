"""Tests for /api/reports/* endpoints."""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "testtoken"
H = {"host": "127.0.0.1:4321"}
TQ = f"token={TOKEN}"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    caps = tmp_path / "capsules" / "run_test001"
    caps.mkdir(parents=True)
    (caps / "capsule.yaml").write_text(
        "run_id: run_test001\nstatus: ok\ncreated_at: 2026-05-01T10:00:00Z\n"
        "finished_at: 2026-05-01T10:00:05Z\nduration_ms: 5000\n"
        "exit_code: 0\nmodel_call_count: 3\ntool_call_count: 2\n"
        "command: ['python', 'agent.py']\nnovafabric_version: 0.30.0\n"
    )
    db = tmp_path / "registry.db"
    app = create_app(capsule_dir=caps.parent, db_path=db, token=TOKEN)
    return TestClient(app)


def test_run_history_json(client: TestClient) -> None:
    r = client.get(f"/api/reports/run-history?{TQ}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body
    assert "columns" in body
    assert body["columns"][0] == "run_id"


def test_run_history_csv(client: TestClient) -> None:
    r = client.get(f"/api/reports/run-history?{TQ}&format=csv", headers=H)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    assert len(rows) >= 1
    assert "run_id" in rows[0]


def test_run_history_filter_status(client: TestClient) -> None:
    r = client.get(f"/api/reports/run-history?{TQ}&status=ok", headers=H)
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert all(row["status"] == "ok" for row in rows)


def test_cost_burn_json(client: TestClient) -> None:
    r = client.get(f"/api/reports/cost-burn?{TQ}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body
    assert body["columns"] == ["agent", "runs", "model_calls", "tool_calls"]


def test_throughput_json(client: TestClient) -> None:
    r = client.get(f"/api/reports/throughput?{TQ}&resolution=1d", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body


def test_executive_summary_json(client: TestClient) -> None:
    r = client.get(f"/api/reports/executive-summary?{TQ}", headers=H)
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert "total_runs" in rows[0]
    assert rows[0]["total_runs"] >= 1


def test_evidence_inventory_json(client: TestClient) -> None:
    r = client.get(f"/api/reports/evidence-inventory?{TQ}", headers=H)
    assert r.status_code == 200
    assert "rows" in r.json()


def test_eval_regression_no_db(client: TestClient) -> None:
    r = client.get(f"/api/reports/eval-regression?{TQ}", headers=H)
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_policy_audit_no_db(client: TestClient) -> None:
    r = client.get(f"/api/reports/policy-audit?{TQ}", headers=H)
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_seal_verification_no_db(client: TestClient) -> None:
    r = client.get(f"/api/reports/seal-verification?{TQ}", headers=H)
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_capsule_compare_json(client: TestClient) -> None:
    r = client.get(f"/api/reports/capsule-compare?{TQ}&run_a=run_test001&run_b=run_test001", headers=H)
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert any(row["field"] == "status" for row in rows)


def test_release_comparison_no_db(client: TestClient) -> None:
    r = client.get(f"/api/reports/release-comparison?{TQ}&version_a=0.1.0&version_b=0.2.0", headers=H)
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_unauthenticated(tmp_path: Path) -> None:
    r = TestClient(
        create_app(capsule_dir=tmp_path, db_path=None, token="secret")
    ).get("/api/reports/run-history", headers=H)
    assert r.status_code == 401
