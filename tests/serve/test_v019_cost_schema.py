"""Tests for v0.19.0 cost + schema dashboard routes.

DB-COST-1 — `/api/cost/pricing`, `/api/cost/report`
DB-SCH-1  — `/api/schema/list`
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

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / "01TEST00000000000000000001"
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.safe_dump({
        "schema_version": "0.1.0",
        "novafabric_version": "0.19.0",
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
    }))
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


# ---------- DB-COST-1 ----------


class TestCostRoutes:
    def test_pricing_returns_known_models(self, client: TestClient) -> None:
        res = client.get(f"/api/cost/pricing?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        models = {row["model"] for row in body["pricing"]}
        assert "gpt-4o" in models
        assert "claude-3-5-sonnet" in models
        # Every row has both prices.
        for row in body["pricing"]:
            assert row["input_price_per_1k_usd"] >= 0
            assert row["output_price_per_1k_usd"] >= 0

    def test_report_duckdb_fallback_when_clickhouse_unconfigured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When NOVA_CLICKHOUSE_URL is unset, /api/cost/report falls back to
        the local DuckDB accumulator (Evidence Fabric local-first mode).
        Response shape is preserved; backend is "duckdb" and ok is True.
        """
        monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
        # Point at an empty DuckDB so no ClickHouse is required.
        monkeypatch.setenv(
            "NOVA_EVIDENCE_DUCKDB_PATH", str(tmp_path / "test_ev.duckdb")
        )
        res = client.get(f"/api/cost/report?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        # Local DuckDB fallback: ok=True, backend="duckdb", empty by_model list.
        assert body["backend"] == "duckdb"
        assert body["ok"] is True
        assert body["totals"]["cost_usd"] == 0.0
        assert isinstance(body["by_model"], list)

    def test_report_with_clickhouse_url_attempts_real_query(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When NOVA_CLICKHOUSE_URL is set the endpoint attempts a real query.

        In CI there is no ClickHouse instance, so the response will either be
        ok=True (if a server is reachable) or ok=False with backend="clickhouse"
        and an "error" key.  Either way the HTTP status must be 200 and the
        backend field must be "clickhouse".
        """
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://clickhouse:8123")
        res = client.get(
            f"/api/cost/report?run_id=run_xyz&days=14&{TOKEN_Q}", headers=HEADERS
        )
        assert res.status_code == 200
        body = res.json()
        assert body["backend"] == "clickhouse"
        assert body["run_id"] == "run_xyz"
        assert body["days"] == 14
        # Either success or graceful error — both are valid in a no-CH env
        assert body.get("ok") in (True, False)

    def test_report_days_validation(self, client: TestClient) -> None:
        # days out of range → 422
        res = client.get(f"/api/cost/report?days=9999&{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 422

    def test_pricing_requires_auth(self, client: TestClient) -> None:
        res = client.get("/api/cost/pricing", headers=HEADERS)
        assert res.status_code == 401


# ---------- DB-SCH-1 ----------


class TestSchemaRoutes:
    def test_list_returns_all_event_types(self, client: TestClient) -> None:
        # 25 baseline (ADR-0066) + 8 extended span taxonomy (ADR-0082) = 33.
        res = client.get(f"/api/schema/list?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert body["event_type_count"] == 33
        assert len(body["event_types"]) == 33
        assert "RunStarted" in body["event_types"]
        assert "ModelCallCompleted" in body["event_types"]
        assert "PIIDetected" in body["event_types"]
        # extended span taxonomy present
        assert "StateTransition" in body["event_types"]
        assert "VectorRetrievalCompleted" in body["event_types"]

    def test_list_returns_categorised_events(self, client: TestClient) -> None:
        res = client.get(f"/api/schema/list?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        cats = body["categories"]
        assert "Run lifecycle" in cats
        assert "RunStarted" in cats["Run lifecycle"]
        assert "Model calls" in cats
        assert "ModelCallCompleted" in cats["Model calls"]

    def test_list_returns_pydantic_models(self, client: TestClient) -> None:
        res = client.get(f"/api/schema/list?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        names = {m["name"] for m in body["pydantic_models"]}
        assert "RunEnvelope" in names
        assert "CostFacet" in names
        for m in body["pydantic_models"]:
            assert isinstance(m["fields"], list)
            assert len(m["fields"]) > 0

    def test_list_schema_version_is_1_0_0(self, client: TestClient) -> None:
        res = client.get(f"/api/schema/list?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        assert res.json()["schema_version"] == "1.0.0"

    def test_list_requires_auth(self, client: TestClient) -> None:
        res = client.get("/api/schema/list", headers=HEADERS)
        assert res.status_code == 401
