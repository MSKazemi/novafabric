"""``POST /api/otlp/v1/logs`` reads its body bounded, like the traces route.

The logs route called ``await request.body()`` and only then compared the length
with ``logs_ingest.MAX_REQUEST_BYTES`` (16 MiB, ADR-0293): the whole body was
already buffered in memory, whatever its size, before the check ran. It now reads
through :func:`novafabric.serve.body_limits.read_body_capped`, which stops at the
cap (with or without an honest ``Content-Length``), and answers 413 past it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from novafabric.otel import logs_ingest  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
FIXTURE = Path(__file__).parents[1] / "fixtures" / "otlp" / "logs-valid.json"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "otlp-logs"
    monkeypatch.setenv("NOVAFABRIC_OTLP_LOG_DIR", str(d))
    return d


@pytest.fixture
def client(tmp_path: Path, store: Path) -> TestClient:
    caps = tmp_path / "runs"
    caps.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=caps, db_path=tmp_path / "r.db"))


def _body() -> bytes:
    return FIXTURE.read_bytes()


def _post(client: TestClient, content: Any, ctype: str = "application/json"):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/otlp/v1/logs?token={TOKEN}",
        content=content,
        headers={**HEADERS, "content-type": ctype},
    )


def test_route_never_buffers_the_whole_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _buffered(self: Request) -> bytes:  # pragma: no cover - the regression
        raise AssertionError("request.body() buffers an unbounded body")

    monkeypatch.setattr(Request, "body", _buffered)
    resp = _post(client, _body())
    assert resp.status_code == 200, resp.text


def test_oversized_body_is_413_and_stores_nothing(
    client: TestClient, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _body()
    monkeypatch.setattr(logs_ingest, "MAX_REQUEST_BYTES", len(body) - 1)
    resp = _post(client, body)
    assert resp.status_code == 413, resp.text
    assert not store.exists() or not any(store.rglob("*.jsonl"))


def test_body_at_the_cap_is_accepted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body()
    monkeypatch.setattr(logs_ingest, "MAX_REQUEST_BYTES", len(body))
    resp = _post(client, body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["records_seen"] >= 1


def test_oversized_chunked_body_without_content_length_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logs_ingest, "MAX_REQUEST_BYTES", 64)

    def chunks() -> Any:
        for _ in range(10):
            yield b"\x00" * 32

    resp = _post(client, chunks(), "application/x-protobuf")
    assert resp.status_code == 413, resp.text


def test_malformed_body_is_still_400(client: TestClient) -> None:
    resp = _post(client, b"{")
    assert resp.status_code == 400
    assert "invalid JSON" in json.loads(resp.text)["detail"]
