from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from novafabric.collector_app.app import CapsuleIngestionRequest, create_collector_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_accumulator(method: str = "ingest_capsule_events", return_val: int = 3) -> MagicMock:
    acc = MagicMock()
    getattr(acc, method)  # ensure attribute exists for hasattr check
    if method == "ingest_events":
        acc.ingest_events = MagicMock(return_value=return_val)
        del acc.ingest_capsule_events  # remove other method so hasattr picks right one
    else:
        acc.ingest_capsule_events = MagicMock(return_value=return_val)
        # Remove ingest_events so the fallback path is taken
        del acc.ingest_events
    return acc


def _duckdb_accumulator() -> Any:
    acc = MagicMock(spec=["ingest_capsule_events"])
    acc.ingest_capsule_events = MagicMock(return_value=2)
    return acc


def _clickhouse_accumulator() -> Any:
    acc = MagicMock(spec=["ingest_events"])
    acc.ingest_events = MagicMock(return_value=5)
    return acc


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_health_returns_ok() -> None:
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /capsule — no auth
# ---------------------------------------------------------------------------


def test_ingest_capsule_duckdb_accumulator() -> None:
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {
        "capsule_id": "cap-001",
        "tenant_id": "tenant-a",
        "events": [{"type": "llm_call"}, {"type": "tool_call"}],
    }
    resp = client.post("/capsule", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["ingested"] == 2
    acc.ingest_capsule_events.assert_called_once()
    enriched = acc.ingest_capsule_events.call_args[0][0]
    assert all(e["run_id"] == "cap-001" for e in enriched)
    assert all(e["tenant"] == "tenant-a" for e in enriched)


def test_ingest_capsule_clickhouse_accumulator() -> None:
    acc = _clickhouse_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {
        "capsule_id": "cap-002",
        "tenant_id": "tenant-b",
        "events": [{"type": "e1"}, {"type": "e2"}, {"type": "e3"}],
    }
    resp = client.post("/capsule", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    acc.ingest_events.assert_called_once()


def test_ingest_capsule_empty_events() -> None:
    acc = _duckdb_accumulator()
    acc.ingest_capsule_events.return_value = 0
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {"capsule_id": "cap-003", "tenant_id": "t", "events": []}
    resp = client.post("/capsule", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ingested"] == 0


def test_ingest_capsule_existing_run_id_not_overwritten() -> None:
    """Events that already have run_id set keep their own value."""
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {
        "capsule_id": "cap-from-body",
        "tenant_id": "t",
        "events": [{"type": "x", "run_id": "pre-existing-id"}],
    }
    resp = client.post("/capsule", json=payload)
    assert resp.status_code == 200
    enriched = acc.ingest_capsule_events.call_args[0][0]
    assert enriched[0]["run_id"] == "pre-existing-id"


# ---------------------------------------------------------------------------
# Auth — NOVA_COLLECTOR_TOKEN
# ---------------------------------------------------------------------------


def test_auth_valid_token_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", "secret-token")
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {"capsule_id": "c", "tenant_id": "t", "events": []}
    resp = client.post(
        "/capsule",
        json=payload,
        headers={"X-Nova-Token": "secret-token"},
    )
    assert resp.status_code == 200


def test_auth_missing_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", "secret-token")
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {"capsule_id": "c", "tenant_id": "t", "events": []}
    resp = client.post("/capsule", json=payload)
    assert resp.status_code == 401


def test_auth_wrong_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_COLLECTOR_TOKEN", "secret-token")
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {"capsule_id": "c", "tenant_id": "t", "events": []}
    resp = client.post(
        "/capsule",
        json=payload,
        headers={"X-Nova-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_auth_disabled_when_no_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_COLLECTOR_TOKEN", raising=False)
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    payload = {"capsule_id": "c", "tenant_id": "t", "events": [{"x": 1}]}
    resp = client.post("/capsule", json=payload)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CapsuleIngestionRequest model
# ---------------------------------------------------------------------------


def test_capsule_ingestion_request_model() -> None:
    req = CapsuleIngestionRequest(
        capsule_id="abc",
        tenant_id="t",
        events=[{"a": 1}],
    )
    assert req.capsule_id == "abc"
    assert len(req.events) == 1


def test_post_capsule_invalid_body_returns_422() -> None:
    acc = _duckdb_accumulator()
    client = TestClient(create_collector_app(accumulator=acc))
    resp = client.post("/capsule", json={"bad": "payload"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# _default_accumulator — env-based selection (import-path smoke test)
# ---------------------------------------------------------------------------


def test_default_accumulator_uses_duckdb_when_no_clickhouse_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    from novafabric.collector_app.app import _default_accumulator

    acc = _default_accumulator()
    assert hasattr(acc, "ingest_capsule_events")


def test_default_accumulator_falls_back_when_clickhouse_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "clickhouse://localhost:9000")
    import sys
    # Simulate clickhouse_connect not installed by temporarily hiding the module
    real_module = sys.modules.pop(
        "novafabric.evidence_fabric.clickhouse_accumulator", None
    )
    try:
        # Force re-import path by clearing cached factory module state
        from novafabric.collector_app.app import _default_accumulator

        acc = _default_accumulator()
        # Should fall back to DuckDB
        assert hasattr(acc, "ingest_capsule_events")
    finally:
        if real_module is not None:
            sys.modules["novafabric.evidence_fabric.clickhouse_accumulator"] = real_module
