"""Tests for the ADR-0182 self-observability wiring on the serve app.

/livez and /readyz are unauthenticated probe endpoints (like /api/health);
/metrics sits behind the existing serve token like every other route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "test-token-1234567890abcdef"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
    )
    return TestClient(app, raise_server_exceptions=False)


def test_livez_returns_200_ok(client: TestClient) -> None:
    resp = client.get("/livez", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_itemized_checks(client: TestClient) -> None:
    resp = client.get("/readyz", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["migrations"] in ("ok", "unknown")
    assert body["checks"]["object_store"] == "skipped"


def test_metrics_requires_token(client: TestClient) -> None:
    resp = client.get("/metrics", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 401


def test_metrics_with_token(client: TestClient) -> None:
    pytest.importorskip("prometheus_client")
    client.get("/livez", headers=LOCALHOST_HEADERS)
    resp = client.get("/metrics", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert 'nova_http_requests_total{app="serve"' in text
    assert 'route="/livez"' in text


def test_api_health_alias_unchanged(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
