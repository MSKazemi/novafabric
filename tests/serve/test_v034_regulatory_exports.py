"""Tests for v0.34.0 regulatory-export API endpoints.

Covers:
  - GET  /api/compliance/euaiact/status
  - POST /api/compliance/euaiact/export
  - POST /api/compliance/export/rocrate
  - POST /api/lineage/export-prov
  - POST /api/compliance/export/c2pa

See src/novafabric/serve/app.py (inserted before the Topology section).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
AUTH = HEADERS
TOKEN_Q = f"token={VALID_TOKEN}"
RUN_ID = "01REGTEST00000000000000001"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / RUN_ID
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.34.0",
        "run_id": RUN_ID,
        "created_at": "2026-05-20T00:00:00+00:00",
        "finished_at": "2026-05-20T00:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print(1)"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
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
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# ---------- GET /api/compliance/euaiact/status ----------


class TestEuAiActStatus:
    def test_returns_200_ok_with_required_fields(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        res = client.get(f"/api/compliance/euaiact/status?{TOKEN_Q}", headers=AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert "high_risk" in body
        assert "provider_mode" in body
        assert "retention_months" in body
        assert body["deadline"] == "2026-08-02"
        assert "ADR-0076" in body["note"]

    def test_defaults_to_deployer_mode_when_env_unset(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        res = client.get(f"/api/compliance/euaiact/status?{TOKEN_Q}", headers=AUTH)
        body = res.json()
        assert body["provider_mode"] is False
        assert body["retention_months"] == 6  # deployer default

    def test_provider_mode_when_env_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_EUAIACT_PROVIDER", "true")
        res = client.get(f"/api/compliance/euaiact/status?{TOKEN_Q}", headers=AUTH)
        body = res.json()
        assert body["provider_mode"] is True
        assert body["retention_months"] == 120  # 10-year floor

    def test_requires_auth(self, client: TestClient) -> None:
        res = client.get("/api/compliance/euaiact/status", headers=HEADERS)
        assert res.status_code == 401


# ---------- POST /api/compliance/euaiact/export ----------


class TestEuAiActExport:
    def test_returns_200_ok_with_empty_capsule_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use an empty capsule dir — no capsules present
        empty_dir = tmp_path / "empty_runs"
        empty_dir.mkdir()
        app = create_app(
            token=VALID_TOKEN,
            capsule_dir=empty_dir,
            db_path=tmp_path / "registry.db",
            static_dir=None,
        )
        with TestClient(app) as c:
            res = c.post(
                f"/api/compliance/euaiact/export?{TOKEN_Q}",
                json={},
                headers=AUTH,
            )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["records"] == []
        assert body["count"] == 0

    def test_returns_records_for_existing_capsule(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        res = client.post(
            f"/api/compliance/euaiact/export?{TOKEN_Q}",
            json={},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["count"] >= 1
        assert body["mode"] == "deployer"
        assert "records" in body
        record = body["records"][0]
        assert record["run_id"] == RUN_ID

    def test_accepts_from_date_to_date_params(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/euaiact/export?{TOKEN_Q}",
            json={"from_date": "2026-01-01T00:00:00", "to_date": "2026-12-31T23:59:59"},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True

    def test_invalid_date_format_returns_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/euaiact/export?{TOKEN_Q}",
            json={"from_date": "not-a-date"},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_requires_auth(self, client: TestClient) -> None:
        res = client.post(
            "/api/compliance/euaiact/export",
            json={},
            headers=HEADERS,
        )
        assert res.status_code == 401


# ---------- POST /api/compliance/export/rocrate ----------


class TestRoCrateExport:
    def test_returns_200_with_zip_base64(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64
        import zipfile

        monkeypatch.setattr(
            "novafabric.compliance.export.ro_crate.export_ro_crate",
            lambda capsule_dir, output_path: _write_minimal_zip(output_path),
        )
        res = client.post(
            f"/api/compliance/export/rocrate?{TOKEN_Q}",
            json={"run_id": RUN_ID},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["run_id"] == RUN_ID
        assert "zip_base64" in body
        assert body["size_bytes"] > 0
        # Decode and verify it's a real zip
        zip_bytes = base64.b64decode(body["zip_base64"])
        assert zipfile.is_zipfile(__import__("io").BytesIO(zip_bytes))

    def test_404_on_unknown_run_id(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/rocrate?{TOKEN_Q}",
            json={"run_id": "nonexistent-run-id"},
            headers=AUTH,
        )
        assert res.status_code == 404

    def test_missing_run_id_returns_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/rocrate?{TOKEN_Q}",
            json={},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_requires_auth(self, client: TestClient) -> None:
        res = client.post(
            "/api/compliance/export/rocrate",
            json={"run_id": RUN_ID},
            headers=HEADERS,
        )
        assert res.status_code == 401


# ---------- POST /api/lineage/export-prov ----------


class TestLineageExportProv:
    def test_returns_200_with_prov_document(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "novafabric.compliance.export.prov_json.export_prov_json",
            lambda capsule_dir: {"prefixes": {}, "entity": {}, "activity": {}, "wasDerivedFrom": {}},
        )
        res = client.post(
            f"/api/lineage/export-prov?{TOKEN_Q}",
            json={"run_id": RUN_ID},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["run_id"] == RUN_ID
        assert "document" in body
        assert "W3C PROV-JSON" in body["note"]

    def test_404_on_unknown_run_id(self, client: TestClient) -> None:
        res = client.post(
            f"/api/lineage/export-prov?{TOKEN_Q}",
            json={"run_id": "nonexistent-run-id"},
            headers=AUTH,
        )
        assert res.status_code == 404

    def test_missing_run_id_returns_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/lineage/export-prov?{TOKEN_Q}",
            json={},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_requires_auth(self, client: TestClient) -> None:
        res = client.post(
            "/api/lineage/export-prov",
            json={"run_id": RUN_ID},
            headers=HEADERS,
        )
        assert res.status_code == 401


# ---------- POST /api/compliance/export/c2pa ----------


class TestC2PAExport:
    def test_returns_200_with_manifest(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "novafabric.evidence.c2pa_exporter.export_c2pa",
            lambda capsule_dir, output_path, **kwargs: _write_c2pa_stub(output_path),
        )
        res = client.post(
            f"/api/compliance/export/c2pa?{TOKEN_Q}",
            json={"run_id": RUN_ID},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["run_id"] == RUN_ID
        assert "manifest" in body
        assert "C2PA" in body["note"]
        assert "ADR-0074" in body["note"]

    def test_include_training_mining_flag(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[bool] = []

        def _fake_export(capsule_dir: Path, output_path: Path, **kwargs: object) -> Path:
            calls.append(bool(kwargs.get("include_training_mining")))
            return _write_c2pa_stub(output_path)

        monkeypatch.setattr("novafabric.evidence.c2pa_exporter.export_c2pa", _fake_export)
        res = client.post(
            f"/api/compliance/export/c2pa?{TOKEN_Q}",
            json={"run_id": RUN_ID, "include_training_mining": True},
            headers=AUTH,
        )
        assert res.status_code == 200
        assert calls == [True]

    def test_404_on_unknown_run_id(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/c2pa?{TOKEN_Q}",
            json={"run_id": "nonexistent-run-id"},
            headers=AUTH,
        )
        assert res.status_code == 404

    def test_missing_run_id_returns_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/c2pa?{TOKEN_Q}",
            json={},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_requires_auth(self, client: TestClient) -> None:
        res = client.post(
            "/api/compliance/export/c2pa",
            json={"run_id": RUN_ID},
            headers=HEADERS,
        )
        assert res.status_code == 401


# ---------- Helpers ----------


def _write_minimal_zip(output_path: Path) -> Path:
    """Write a minimal valid ZIP to *output_path* and return it."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ro-crate-metadata.json", json.dumps({"@context": "https://w3id.org/ro/crate/1.1/context"}))
    output_path.write_bytes(buf.getvalue())
    return output_path


def _write_c2pa_stub(output_path: Path) -> Path:
    """Write a minimal C2PA manifest stub JSON and return path."""
    output_path.write_text(json.dumps({
        "claim_generator": "NovaFabric/0.34.0",
        "assertions": [],
        "schema_version": "2.3",
    }))
    return output_path
