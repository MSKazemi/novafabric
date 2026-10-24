"""``GET /api/storage/validate`` never sends the server's S3 credentials to a caller's host
(review serve-13).

The route built an S3 client for any ``endpoint`` the caller named, with the server's
credentials — its SigV4 ``Authorization`` header (access key id, signature) went to
that host: an SSRF on a read-scope route. A caller endpoint is accepted only when it
is the configured ``NOVA_S3_ENDPOINT_URL``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "storage-validate-token"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    endpoints: list[str | None] = []

    class _Store:
        def __init__(self, endpoint_url: str | None, bucket: str) -> None:
            endpoints.append(endpoint_url)

        def validate(self) -> dict[str, Any]:
            return {"object_lock": "COMPLIANCE"}

    pytest.importorskip("novafabric.storage.nova_object_store")
    monkeypatch.setattr("novafabric.storage.nova_object_store.NovaObjectStore", _Store)
    return endpoints


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    store = tmp_path / "runs"
    store.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r.db"))


def test_a_foreign_endpoint_is_refused_without_contacting_it(
    client: TestClient, built: list[str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_S3_ENDPOINT_URL", "https://s3.internal:9000")
    body = client.get("/api/storage/validate", headers=HEADERS,
                      params={"endpoint": "http://attacker.example:80"}).json()
    assert body["ok"] is False and body["error_class"] == "EndpointNotAllowed"
    assert built == []


def test_the_configured_endpoint_is_validated(
    client: TestClient, built: list[str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_S3_ENDPOINT_URL", "https://s3.internal:9000")
    for params in ({}, {"endpoint": "https://s3.internal:9000"}):
        body = client.get("/api/storage/validate", headers=HEADERS, params=params).json()
        assert body["ok"] is True, body
    assert built == ["https://s3.internal:9000", "https://s3.internal:9000"]


def test_with_no_configured_endpoint_a_caller_endpoint_is_refused(
    client: TestClient, built: list[str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_S3_ENDPOINT_URL", raising=False)
    body = client.get("/api/storage/validate", headers=HEADERS,
                      params={"endpoint": "http://127.0.0.1:1"}).json()
    assert body["ok"] is False and built == []
