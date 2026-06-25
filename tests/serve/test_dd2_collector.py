"""Tests for DD-2: /api/infra/collector endpoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import _parse_prom_gauge, create_app

TOKEN = "testtoken"
AUTH = {"token": TOKEN}
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db_path = tmp_path / "registry.db"
    app = create_app(token=TOKEN, capsule_dir=capsule_dir, db_path=db_path)
    return TestClient(app)


# ---------- /api/infra/collector ----------


def test_collector_not_detected_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Returns {detected: false} when no health file exists and collector is not running."""
    monkeypatch.setenv("NOVA_COLLECTOR_HEALTH_FILE", str(tmp_path / "no-such-file.json"))
    monkeypatch.setenv("NOVA_COLLECTOR_METRICS_URL", "http://127.0.0.1:19999/metrics")
    resp = client.get("/api/infra/collector", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected"] is False


def test_collector_requires_token(client: TestClient) -> None:
    resp = client.get("/api/infra/collector", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 401


def test_collector_detected_from_health_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Returns {detected: true} when a valid health file is present."""
    health_file = tmp_path / "collector-health.json"
    health_file.write_text(
        json.dumps({
            "spool_lag": 42,
            "signing_p99_ms": 15.3,
            "events_per_sec": 1234.5,
            "last_heartbeat": "2026-05-14T10:00:00Z",
            "version": "0.1.0",
        })
    )
    monkeypatch.setenv("NOVA_COLLECTOR_HEALTH_FILE", str(health_file))
    resp = client.get("/api/infra/collector", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected"] is True
    assert data["spool_lag"] == 42
    assert data["signing_p99_ms"] == pytest.approx(15.3)
    assert data["events_per_sec"] == pytest.approx(1234.5)
    assert data["last_heartbeat"] == "2026-05-14T10:00:00Z"
    assert data["collector_version"] == "0.1.0"
    assert data["source"] == str(health_file)


def test_collector_health_file_missing_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Partial health file still returns detected=true with None for missing fields."""
    health_file = tmp_path / "collector-health.json"
    health_file.write_text(json.dumps({"spool_lag": 0}))
    monkeypatch.setenv("NOVA_COLLECTOR_HEALTH_FILE", str(health_file))
    resp = client.get("/api/infra/collector", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected"] is True
    assert data["spool_lag"] == 0
    assert data["signing_p99_ms"] is None
    assert data["events_per_sec"] is None
    assert data["collector_version"] == "unknown"


def test_collector_health_file_corrupt_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Corrupt health file is silently skipped, falls through to detected=false."""
    health_file = tmp_path / "collector-health.json"
    health_file.write_text("not valid json {{{")
    monkeypatch.setenv("NOVA_COLLECTOR_HEALTH_FILE", str(health_file))
    monkeypatch.setenv("NOVA_COLLECTOR_METRICS_URL", "http://127.0.0.1:19999/metrics")
    resp = client.get("/api/infra/collector", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected"] is False


# ---------- _parse_prom_gauge ----------


def test_parse_prom_gauge_simple() -> None:
    text = "# HELP nova_spool_queue_depth Spool queue depth\nnova_spool_queue_depth 37\n"
    assert _parse_prom_gauge(text, "nova_spool_queue_depth") == pytest.approx(37.0)


def test_parse_prom_gauge_with_labels() -> None:
    text = 'nova_events_processed_total{node="n1"} 99000\n'
    assert _parse_prom_gauge(text, "nova_events_processed_total") == pytest.approx(99000.0)


def test_parse_prom_gauge_missing_metric() -> None:
    text = "nova_other_metric 1\n"
    assert _parse_prom_gauge(text, "nova_spool_queue_depth") is None


def test_parse_prom_gauge_empty_text() -> None:
    assert _parse_prom_gauge("", "nova_spool_queue_depth") is None


def test_parse_prom_gauge_comment_lines_skipped() -> None:
    text = "# TYPE nova_spool_queue_depth gauge\n# HELP nova_spool_queue_depth depth\n"
    assert _parse_prom_gauge(text, "nova_spool_queue_depth") is None
