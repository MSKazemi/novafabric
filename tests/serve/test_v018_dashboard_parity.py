"""Tests for v0.18.0 dashboard-parity routes.

Covers DB-KG-1 (Capsule KG), DB-CAP-1 (capture-level), DB-ERA-1 (GDPR
erasure), DB-STG-1 (storage validate/inspect). All extend the existing
`nova serve` API surface. See `.claude/plans/v018-dashboard-parity-for-v017-features.md`.
"""

from __future__ import annotations

import json
import os
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
AUTH = HEADERS  # auth is via ?token=... query param, not headers
TOKEN_Q = f"token={VALID_TOKEN}"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / "01TEST00000000000000000001"
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.18.0",
        "run_id": "01TEST00000000000000000001",
        "created_at": "2026-05-18T00:00:00+00:00",
        "finished_at": "2026-05-18T00:00:01+00:00",
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


# ---------- DB-KG-1: KG routes ----------


class TestKGRoutes:
    def test_status_returns_not_initialised_without_store(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_KG_PATH", str(tmp_path / "kg" / "missing.kuzu"))
        res = client.get(f"/api/kg/status?{TOKEN_Q}", headers=AUTH)
        if res.status_code == 501:
            pytest.skip("kuzu not installed in this environment")
        assert res.status_code == 200
        body = res.json()
        assert body["store_health"] == "not_initialised"
        assert body["edge_count"] == 0

    def test_status_returns_ok_after_init(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kuzu = pytest.importorskip("kuzu")  # noqa: F841
        from novafabric.kg.store import KGStore

        db_path = tmp_path / "kg" / "store.kuzu"
        store = KGStore(db_path)
        store.init_schema()
        del store  # close handle
        monkeypatch.setenv("NOVA_KG_PATH", str(db_path))
        res = client.get(f"/api/kg/status?{TOKEN_Q}", headers=AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["store_health"] == "ok"
        assert body["edge_count"] == 0

    def test_agent_edges_returns_empty_when_no_store(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_KG_PATH", str(tmp_path / "kg" / "none.kuzu"))
        res = client.get(f"/api/kg/agents/agent-xyz/edges?{TOKEN_Q}", headers=AUTH)
        if res.status_code == 501:
            pytest.skip("kuzu not installed in this environment")
        assert res.status_code == 200
        body = res.json()
        assert body["agent_id"] == "agent-xyz"
        assert body["models"] == []
        assert body["tools"] == []
        assert body["mcp_servers"] == []

    def test_agent_edges_returns_data_after_ingest(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("kuzu")
        from novafabric.kg.store import KGStore

        db_path = tmp_path / "kg" / "with-data.kuzu"
        store = KGStore(db_path)
        store.init_schema()
        store.merge_agent("agent-a", "Agent A")
        store.merge_model("openai/gpt-4o", provider="openai", canonical_name="gpt-4o")
        store.upsert_calls_edge(
            agent_id="agent-a",
            model_id="openai/gpt-4o",
            call_count=3,
            verified_count=2,
            confidence=2 / 3,
            capsule_id="cap-001",
        )
        del store
        monkeypatch.setenv("NOVA_KG_PATH", str(db_path))
        res = client.get(f"/api/kg/agents/agent-a/edges?{TOKEN_Q}", headers=AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["agent_id"] == "agent-a"
        assert len(body["models"]) == 1
        assert body["models"][0]["model_id"] == "openai/gpt-4o"
        assert body["models"][0]["call_count"] == 3
        assert "mcp_servers" in body

    def test_kg_status_requires_auth(self, client: TestClient) -> None:
        res = client.get("/api/kg/status", headers=HEADERS)
        assert res.status_code == 401


# ---------- DB-CAP-1: capture-level routes ----------


class TestCaptureLevelRoutes:
    def test_get_returns_current_level(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")
        res = client.get(f"/api/policy/capture-level?{TOKEN_Q}", headers=AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["current_level"] == "standard"
        assert "tiers" in body
        assert set(body["tiers"].keys()) == {
            "minimal",
            "standard",
            "forensic",
            "air_gapped",
        }

    def test_get_defaults_to_standard_when_env_unset(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_CAPTURE_LEVEL", raising=False)
        res = client.get(f"/api/policy/capture-level?{TOKEN_Q}", headers=AUTH)
        assert res.status_code == 200
        assert res.json()["current_level"] == "standard"

    def test_post_valid_level_returns_instructions(self, client: TestClient) -> None:
        res = client.post(
            f"/api/policy/capture-level?{TOKEN_Q}",
            json={"level": "forensic"},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["level"] == "forensic"
        assert "NOVA_CAPTURE_LEVEL=forensic" in body["instruction"]

    def test_post_invalid_level_returns_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/policy/capture-level?{TOKEN_Q}",
            json={"level": "absurd"},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_post_missing_body_returns_422(self, client: TestClient) -> None:
        res = client.post(f"/api/policy/capture-level?{TOKEN_Q}", json={}, headers=AUTH)
        assert res.status_code == 422


# ---------- DB-ERA-1: GDPR erasure routes ----------


class TestErasureRoutes:
    def test_request_pending_when_cap003_enabled(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAP003_ENABLED", "true")
        res = client.post(
            f"/api/compliance/erasure/request?{TOKEN_Q}",
            json={"subject_id": "subject-001", "reason": "gdpr_art_17"},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["state"] == "PENDING"
        assert body["cap003_enabled"] is True

    def test_request_flagged_off_when_cap003_disabled(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAP003_ENABLED", "false")
        res = client.post(
            f"/api/compliance/erasure/request?{TOKEN_Q}",
            json={"subject_id": "subject-001"},
            headers=AUTH,
        )
        assert res.status_code == 200
        assert res.json()["state"] == "FEATURE_FLAG_OFF"

    def test_request_missing_subject_returns_422(self, client: TestClient) -> None:
        res = client.post(
            f"/api/compliance/erasure/request?{TOKEN_Q}", json={}, headers=AUTH
        )
        assert res.status_code == 422

    def test_status_returns_empty_list(self, client: TestClient) -> None:
        res = client.get(
            f"/api/compliance/erasure/status?subject_id=sub-1&{TOKEN_Q}", headers=AUTH
        )
        assert res.status_code == 200
        assert res.json()["requests"] == []


# ---------- DB-STG-1: storage routes ----------


class TestStorageRoutes:
    def test_validate_returns_error_on_unreachable_endpoint(
        self, client: TestClient
    ) -> None:
        # boto3 will fail to validate without a real S3; we expect a structured
        # error response rather than a 500.
        res = client.get(
            f"/api/storage/validate?endpoint=http://127.0.0.1:1&bucket=nova-test&{TOKEN_Q}",
            headers=AUTH,
        )
        if res.status_code == 501:
            pytest.skip("boto3 not installed")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert "error" in body

    def test_inspect_returns_object_keys_with_flag_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAP003_ENABLED", "false")
        res = client.get(
            f"/api/storage/inspect/run-abc-123?{TOKEN_Q}", headers=AUTH
        )
        assert res.status_code == 200
        body = res.json()
        assert body["audit_object_key"] == "run-abc-123_audit.json"
        assert body["pii_object_key"] is None
        assert body["cap003_enabled"] is False

    def test_inspect_returns_pii_key_with_flag_on(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAP003_ENABLED", "true")
        res = client.get(
            f"/api/storage/inspect/run-abc-123?{TOKEN_Q}", headers=AUTH
        )
        assert res.status_code == 200
        body = res.json()
        assert body["pii_object_key"] == "run-abc-123_pii.json"
        assert body["cap003_enabled"] is True


@pytest.fixture(autouse=True)
def _isolate_cap003_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure NOVA_CAP003_ENABLED is unset by default per test."""
    monkeypatch.delenv("NOVA_CAP003_ENABLED", raising=False)
    monkeypatch.delenv("NOVA_KG_PATH", raising=False)
    yield
    # Defensive cleanup (monkeypatch auto-restores but be explicit for parallel runs)
    os.environ.pop("NOVA_CAP003_ENABLED", None)
    os.environ.pop("NOVA_KG_PATH", None)
