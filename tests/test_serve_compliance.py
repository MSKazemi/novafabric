"""Tests for compliance API endpoints added in BQ-005 dashboard integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-compliance-token-1234"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    run_id = "01COMPLIANCE000000000001"
    cdir = base / run_id
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.15.0",
        "run_id": run_id,
        "created_at": "2026-05-17T10:00:00+00:00",
        "finished_at": "2026-05-17T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('hi')"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 1,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "name": "test", "kind": "internal"}) + "\n"
    )
    (cdir / "model-calls.jsonl").write_text("")
    (cdir / "tool-calls.jsonl").write_text("")
    return base


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def client(capsule_dir: Path, db_path: Path) -> TestClient:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=db_path,
        static_dir=None,
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------- Tool permission events ----------

def test_tool_permission_events_no_db_returns_empty(client: TestClient, tmp_path: Path) -> None:
    run_id = "01COMPLIANCE000000000001"
    r = client.get(
        f"/api/runs/{run_id}/tool-permission-events",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["events"] == []


def test_tool_permission_events_with_index(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.compliance.tool_permission.index import PermissionEventIndex
    from novafabric.compliance.tool_permission.models import ToolPermissionEvent

    db_path = tmp_path / "compliance" / "tool_permission_idx.db"
    db_path.parent.mkdir(parents=True)
    idx = PermissionEventIndex(db_path)
    with idx:
        event = ToolPermissionEvent(
            capsule_id="01COMPLIANCE000000000001",
            event_id="evt-001",
            tool_name="write_file",
            tool_id="tool-001",
            policy_id="policy-001",
            permission_level="filesystem",
            decision="allowed",
            authorising_identity="system",
            human_approval_required=False,
            human_approval_obtained=None,
            approval_principal=None,
            decision_latency_ms=3,
            capsule_timestamp_utc="2026-05-17T10:00:00Z",
        )
        idx.record(event)

    monkeypatch.setenv("NOVAFABRIC_TOOL_PERMISSION_DB_PATH", str(db_path))

    run_id = "01COMPLIANCE000000000001"
    r = client.get(
        f"/api/runs/{run_id}/tool-permission-events",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["events"]) == 1
    ev = body["events"][0]
    assert ev["tool_name"] == "write_file"
    assert ev["decision"] == "allowed"
    assert ev["permission_level"] == "filesystem"


def test_tool_permission_events_requires_auth(client: TestClient) -> None:
    r = client.get(
        "/api/runs/01COMPLIANCE000000000001/tool-permission-events",
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 401


# ---------- Compliance exports ----------

def test_annex_iv_404_on_unknown_run(client: TestClient) -> None:
    r = client.get(
        "/api/compliance/annex-iv",
        params={"token": VALID_TOKEN, "run_id": "UNKNOWN", "deployment_id": "dep-001"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_annex_iv_requires_auth(client: TestClient) -> None:
    r = client.get(
        "/api/compliance/annex-iv",
        params={"run_id": "01COMPLIANCE000000000001", "deployment_id": "dep-001"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 401


def test_annex_iv_returns_document(client: TestClient) -> None:
    pytest.importorskip("novafabric.compliance.export.annex_iv")
    run_id = "01COMPLIANCE000000000001"
    r = client.get(
        "/api/compliance/annex-iv",
        params={"token": VALID_TOKEN, "run_id": run_id, "deployment_id": "dep-001"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["deployment_id"] == "dep-001"
    assert "document" in body
    assert "complete_elements" in body
    assert "total_elements" in body


def test_nis2_404_on_unknown_run(client: TestClient) -> None:
    r = client.get(
        "/api/compliance/nis2",
        params={"token": VALID_TOKEN, "run_id": "UNKNOWN", "incident_id": "INC-001"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_nis2_rejects_invalid_phase(client: TestClient) -> None:
    run_id = "01COMPLIANCE000000000001"
    r = client.get(
        "/api/compliance/nis2",
        params={"token": VALID_TOKEN, "run_id": run_id, "incident_id": "INC-001", "phase": 9},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 422


def test_nis2_returns_report(client: TestClient) -> None:
    pytest.importorskip("novafabric.compliance.export.nis2")
    run_id = "01COMPLIANCE000000000001"
    r = client.get(
        "/api/compliance/nis2",
        params={"token": VALID_TOKEN, "run_id": run_id, "incident_id": "INC-001", "phase": 1},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["incident_id"] == "INC-001"
    assert body["phase"] == 1
    assert "report" in body


def test_subject_proof_requires_auth(client: TestClient) -> None:
    r = client.get(
        "/api/compliance/subject-proof",
        params={"subject_id": "user@example.com"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 401


def test_subject_proof_503_without_pepper(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_PII_PEPPER", raising=False)
    r = client.get(
        "/api/compliance/subject-proof",
        params={"token": VALID_TOKEN, "subject_id": "user@example.com"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 503


def test_subject_proof_empty_when_no_index(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_PII_PEPPER", "test-pepper-value")
    r = client.get(
        "/api/compliance/subject-proof",
        params={"token": VALID_TOKEN, "subject_id": "user@example.com"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert "subject_id_hmac" in body
    assert body["subject_id_hmac"].startswith("sha256:")
    assert body["records"] == []


def test_subject_proof_with_records(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers the lookup_subject path when the redaction index exists with records."""
    import hashlib
    import hmac as _hmac

    from novafabric.compliance.pii.index import RedactionSubjectIndex

    pepper = b"test-pepper-value"
    subject_id = "user@example.com"
    mac = _hmac.new(pepper, subject_id.encode("utf-8"), hashlib.sha256)
    subject_hmac = "sha256:" + mac.hexdigest()

    nova_home = tmp_path / "nova"
    db_dir = nova_home / "compliance"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "redaction_subject_idx.db"
    with RedactionSubjectIndex(db_path) as idx:
        idx.record(
            subject_id_hmac=subject_hmac,
            capsule_id="CAP-001",
            field_path="payload.user_email",
            legal_basis="GDPR Art.17",
            redacted_at_utc="2026-05-17T10:00:00Z",
        )

    monkeypatch.setenv("NOVA_PII_PEPPER", "test-pepper-value")
    monkeypatch.setenv("NOVAFABRIC_HOME", str(nova_home))

    r = client.get(
        "/api/compliance/subject-proof",
        params={"token": VALID_TOKEN, "subject_id": subject_id},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["subject_id_hmac"] == subject_hmac
    assert len(body["records"]) == 1
    rec = body["records"][0]
    assert rec["capsule_id"] == "CAP-001"
    assert rec["field_path"] == "payload.user_email"
    assert "generated_at" in body


def test_annex_iv_raises_422_on_export_failure(
    client: TestClient, capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers the generic exception branch in annex-iv export."""
    from unittest.mock import MagicMock, patch

    run_id = "01COMPLIANCE000000000001"
    mock_exporter = MagicMock()
    mock_exporter.build_annex_iv_document.side_effect = ValueError("export boom")

    with patch("novafabric.compliance.export.annex_iv.AnnexIVExporter", return_value=mock_exporter):
        r = client.get(
            "/api/compliance/annex-iv",
            params={"token": VALID_TOKEN, "run_id": run_id, "deployment_id": "dep-001"},
            headers=LOCALHOST_HEADERS,
        )
    assert r.status_code == 422
    assert "export boom" in r.json().get("detail", "")


def test_nis2_raises_422_on_export_failure(
    client: TestClient, capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers the generic exception branch in nis2 export."""
    from unittest.mock import MagicMock, patch

    run_id = "01COMPLIANCE000000000001"
    mock_exporter = MagicMock()
    mock_exporter.build_nis2_report.side_effect = RuntimeError("nis2 boom")

    with patch("novafabric.compliance.export.nis2.NIS2Exporter", return_value=mock_exporter):
        r = client.get(
            "/api/compliance/nis2",
            params={"token": VALID_TOKEN, "run_id": run_id, "incident_id": "INC-001", "phase": 2},
            headers=LOCALHOST_HEADERS,
        )
    assert r.status_code == 422
    assert "nis2 boom" in r.json().get("detail", "")


# ---------- GDPR RoPA export (nova export-ropa) ----------

def test_export_ropa_returns_document(client: TestClient, capsule_dir: Path) -> None:
    run_id = "01COMPLIANCE000000000001"
    r = client.post(
        "/api/compliance/export/ropa",
        json={"run_id": run_id, "controller_name": "ACME Corp", "controller_contact": "dpo@acme.example"},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["run_id"] == run_id
    assert "completeness" in body
    assert "document" in body
    assert isinstance(body["missing_fields"], list)


def test_export_ropa_missing_run_id(client: TestClient) -> None:
    r = client.post(
        "/api/compliance/export/ropa",
        json={},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 422


# ---------- AI-SBOM export (nova export-aibom) ----------

def test_export_aibom_returns_bom(client: TestClient, capsule_dir: Path) -> None:
    run_id = "01COMPLIANCE000000000001"
    r = client.post(
        "/api/compliance/export/aibom",
        json={"run_id": run_id},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["run_id"] == run_id
    assert body["bom_format"] == "CycloneDX"
    assert isinstance(body["components"], list)
    assert isinstance(body["component_count"], int)


def test_export_aibom_missing_run_id(client: TestClient) -> None:
    r = client.post(
        "/api/compliance/export/aibom",
        json={},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 422


# ---------- NIST AI RMF export (nova export-nist-rmf) ----------

def test_export_nist_rmf_returns_report(client: TestClient, capsule_dir: Path) -> None:
    run_id = "01COMPLIANCE000000000001"
    r = client.post(
        "/api/compliance/export/nist-rmf",
        json={"run_id": run_id},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["run_id"] == run_id
    assert "risk_level" in body
    assert isinstance(body["metrics"], list)
    assert isinstance(body["missing_evidence"], list)


def test_export_nist_rmf_missing_run_id(client: TestClient) -> None:
    r = client.post(
        "/api/compliance/export/nist-rmf",
        json={},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 422


# ---------- AIBOM status (nova aibom status) ----------

def test_aibom_status_no_capsules(client: TestClient) -> None:
    r = client.get(
        "/api/aibom/status",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "coverage_status" in body
    assert "cra_deadline" in body
    assert "total_capsules" in body


def test_aibom_status_counts_aibom_files(
    client: TestClient, capsule_dir: Path
) -> None:
    run_id = "01COMPLIANCE000000000001"
    aibom_file = capsule_dir / run_id / "aibom.json"
    aibom_file.write_text('{"bomFormat": "CycloneDX"}')
    r = client.get(
        "/api/aibom/status",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["capsules_with_aibom"] >= 1
    assert body["coverage_status"] in {"partial", "complete"}
