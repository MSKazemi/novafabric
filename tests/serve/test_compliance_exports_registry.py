"""Tests for the generic compliance-export registry (ADR-0200 §2).

Covers:
  - GET  /api/compliance/export/kinds  — catalog completeness + field shapes
  - POST /api/compliance/export/{kind} — happy paths, auth, unknown kind,
    missing required field, builder refusal, optional run_id resolution,
    and the audit-log record every export writes.

See src/novafabric/serve/routers/compliance_exports.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.routers.compliance_exports import EXPORT_KINDS  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"
RUN_ID = "01EXPREGTEST00000000000001"

WAVE_A_KINDS = {
    "foia",
    "whistleblower",
    "election-disclosure",
    "transparency-register",
    "accessibility-claim",
    "citizen-explanation",
    "public-incident",
    "public-annex-viii",
    "public-disclosure",
    "control-attestation",
    "rai-scorecard",
    "part11",
    "model-risk",
}


@pytest.fixture
def audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "dashboard-audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(path))
    return path


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / RUN_ID
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.63.0",
        "run_id": RUN_ID,
        "created_at": "2026-07-24T00:00:00+00:00",
        "finished_at": "2026-07-24T00:00:01+00:00",
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
    (cdir / "trace.jsonl").write_text("")
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path, audit_file: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _audit_entries(audit_file: Path) -> list[dict]:
    if not audit_file.exists():
        return []
    return [json.loads(line) for line in audit_file.read_text().splitlines() if line.strip()]


# ---------- GET /api/compliance/export/kinds ----------


class TestKindsCatalog:
    def test_requires_token(self, client: TestClient) -> None:
        res = client.get("/api/compliance/export/kinds", headers=HEADERS)
        assert res.status_code == 401

    def test_all_wave_a_kinds_present(self, client: TestClient) -> None:
        res = client.get(f"/api/compliance/export/kinds?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        kinds = {k["kind"] for k in body["kinds"]}
        assert kinds == WAVE_A_KINDS
        assert body["count"] == len(WAVE_A_KINDS)

    def test_registry_matches_catalog(self) -> None:
        assert set(EXPORT_KINDS) == WAVE_A_KINDS

    def test_fields_are_well_formed(self, client: TestClient) -> None:
        res = client.get(f"/api/compliance/export/kinds?{TOKEN_Q}", headers=HEADERS)
        for entry in res.json()["kinds"]:
            assert entry["label"].strip()
            assert entry["cli_equivalent"].startswith("nova export-")
            assert entry["output"] == "document"  # no Wave-A kind emits a zip
            assert entry["note"].strip()
            assert isinstance(entry["fields"], list) and entry["fields"]
            for f in entry["fields"]:
                assert set(f) == {"key", "label", "type", "required"}
                assert f["type"] in {"string", "boolean", "json"}
                assert isinstance(f["required"], bool)


# ---------- POST /api/compliance/export/{kind} ----------


class TestRunExport:
    def test_requires_token(self, client: TestClient) -> None:
        res = client.post(
            "/api/compliance/export/foia",
            json={"decision_ref": "dec-1"},
            headers=HEADERS,
        )
        assert res.status_code == 401

    def test_unknown_kind_404(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/not-a-kind?{TOKEN_Q}", json={}, headers=HEADERS
        )
        assert res.status_code == 404
        assert "unknown export kind" in res.json()["detail"]

    def test_missing_required_field_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/foia?{TOKEN_Q}", json={}, headers=HEADERS
        )
        assert res.status_code == 422
        assert "decision_ref" in res.json()["detail"]

    def test_foia_happy_path(self, client: TestClient, audit_file: Path) -> None:
        res = client.post(
            f"/api/compliance/export/foia?{TOKEN_Q}",
            json={
                "decision_ref": "dec-2026-001",
                "record_index": ["sha256:aa", "sha256:bb"],
                "redactions": [{"digest": "sha256:cc", "exemption_ref": "5 USC 552(b)(6)"}],
            },
            headers=HEADERS,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["kind"] == "foia"
        assert body["run_id"] is None
        doc = body["document"]
        assert doc["decision_ref"] == "dec-2026-001"
        assert doc["record_index"] == ["sha256:aa", "sha256:bb"]
        assert len(doc["redactions"]) == 1
        assert doc["custody_digest"]
        assert body["cli_equivalent"].startswith("nova export-foia")
        # audit-logged with cli_equivalent
        entries = [e for e in _audit_entries(audit_file) if e["action"] == "export_foia"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "ok"
        assert entry["cli_equivalent"].startswith("nova export-foia")
        assert entry["args"]["kind"] == "foia"
        # payload values never enter the audit log — field keys only
        assert "dec-2026-001" not in json.dumps(entry["args"])

    def test_model_risk_happy_path(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/model-risk?{TOKEN_Q}",
            json={
                "model_id": "credit-scorer-v3",
                "development": ["cap:sha256:dd"],
                "independent_validation": [],
                "ongoing_monitoring": ["cap:sha256:ee"],
                "partial": ["ongoing_monitoring"],
            },
            headers=HEADERS,
        )
        assert res.status_code == 200
        doc = res.json()["document"]
        assert doc["model_id"] == "credit-scorer-v3"
        statuses = {p["pillar"]: p["status"] for p in doc["pillars"]}
        assert statuses["development"] == "complete"
        assert statuses["independent_validation"] == "missing"
        assert statuses["ongoing_monitoring"] == "partial"

    def test_part11_happy_path(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/part11?{TOKEN_Q}",
            json={
                "capsule_root": "sha256:" + "a" * 64,
                "elements": {"record_integrity": "seal:sha256:ff"},
            },
            headers=HEADERS,
        )
        assert res.status_code == 200
        doc = res.json()["document"]
        assert doc["capsule_root"].startswith("sha256:")
        assert doc["banner"]  # the binding medical-honesty banner (ADR-0160)
        statuses = {f["element"]: f["status"] for f in doc["fields"]}
        assert statuses["record_integrity"] == "complete"

    def test_transparency_register_happy_path(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/transparency-register?{TOKEN_Q}",
            json={
                "capsule_root": "sha256:" + "b" * 64,
                "standard": "atrs",
                "operator_declared": {"name": "Benefit triage assistant"},
            },
            headers=HEADERS,
        )
        assert res.status_code == 200
        doc = res.json()["document"]
        assert doc["standard"] == "atrs"
        assert doc["status"] == "DRAFT"

    def test_builder_refusal_maps_to_400(
        self, client: TestClient, audit_file: Path
    ) -> None:
        # Whistleblower source protection (I-5): a source-identifying field
        # is refused by the builder, surfaced as 400, and audit-logged.
        res = client.post(
            f"/api/compliance/export/whistleblower?{TOKEN_Q}",
            json={
                "content_digest": "sha256:" + "c" * 64,
                "authenticity_attestation": "bundle:sig:1",
                "submitter_email": "leak@example.com",
            },
            headers=HEADERS,
        )
        assert res.status_code == 400
        assert "source-identifying" in res.json()["detail"]
        entries = [
            e for e in _audit_entries(audit_file) if e["action"] == "export_whistleblower"
        ]
        assert entries and entries[-1]["result"] == "error"

    def test_invalid_enum_maps_to_400(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/election-disclosure?{TOKEN_Q}",
            json={
                "content_ref": "cid:1",
                "provenance_receipt_ref": "c2pa:sha256:11",
                "disclosure_label": "totally-real",
            },
            headers=HEADERS,
        )
        assert res.status_code == 400
        assert "disclosure_label" in res.json()["detail"]

    def test_json_field_rejects_non_json_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/rai-scorecard?{TOKEN_Q}",
            json={"evidence": "{not json"},
            headers=HEADERS,
        )
        assert res.status_code == 422
        assert "evidence" in res.json()["detail"]

    def test_optional_run_id_resolves_and_echoes(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/public-incident?{TOKEN_Q}",
            json={"run_id": RUN_ID, "incident_ref": "inc-7"},
            headers=HEADERS,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["run_id"] == RUN_ID
        assert body["document"]["draft"] is True

    def test_unknown_run_id_404(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/export/public-incident?{TOKEN_Q}",
            json={"run_id": "01DOESNOTEXIST000000000000", "incident_ref": "inc-7"},
            headers=HEADERS,
        )
        assert res.status_code == 404

    def test_every_kind_minimal_happy_or_clean_error(self, client: TestClient) -> None:
        """Sweep: every registered kind answers with 200 or a clean 4xx (never 5xx)."""
        minimal: dict[str, dict] = {
            "foia": {"decision_ref": "d"},
            "whistleblower": {
                "content_digest": "sha256:" + "d" * 64,
                "authenticity_attestation": "sig:1",
            },
            "election-disclosure": {
                "content_ref": "c",
                "provenance_receipt_ref": "p",
                "disclosure_label": "ai_generated",
            },
            "transparency-register": {"capsule_root": "sha256:" + "e" * 64},
            "accessibility-claim": {"declared_standard": "wcag_2_2_aa"},
            "citizen-explanation": {
                "decision_ref": "d",
                "human_involvement": "human_reviewed",
            },
            "public-incident": {"incident_ref": "i"},
            "public-annex-viii": {"capsule_root": "sha256:" + "f" * 64},
            "public-disclosure": {},
            "control-attestation": {"capsule_root": "sha256:" + "1" * 64},
            "rai-scorecard": {},
            "part11": {"capsule_root": "sha256:" + "2" * 64},
            "model-risk": {"model_id": "m"},
        }
        assert set(minimal) == WAVE_A_KINDS
        for kind, body in minimal.items():
            res = client.post(
                f"/api/compliance/export/{kind}?{TOKEN_Q}", json=body, headers=HEADERS
            )
            assert res.status_code == 200, f"{kind}: {res.status_code} {res.text}"
            payload = res.json()
            assert payload["ok"] is True
            assert isinstance(payload["document"], dict)
