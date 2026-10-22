"""``POST /api/otlp/v1/traces`` has the same body cap as the logs route.

Audit "unconfirmed" item, confirmed by reading: the logs route refuses bodies over
``logs_ingest.MAX_REQUEST_BYTES`` (16 MiB, ADR-0293), but the traces route called
``await request.json()`` / ``await request.body()`` on whatever arrived — no cap
there and no global body limit anywhere in ``serve/`` — so one request could make
the dashboard buffer an arbitrarily large body in memory. The traces route now
reads at most the cap (streamed, so the bound holds with or without an honest
``Content-Length``) and answers 413 past it, writing nothing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.otel import genai_ingest, logs_ingest  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.body_limits import read_body_capped  # noqa: E402

TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
FIXTURES = Path(__file__).parents[1] / "fixtures" / "otlp"


@pytest.fixture
def capsules(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def client(capsules: Path, tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(token=TOKEN, capsule_dir=capsules, db_path=tmp_path / "registry.db")
    )


def test_traces_cap_matches_the_logs_cap() -> None:
    assert genai_ingest.MAX_OTLP_TRACES_BYTES == logs_ingest.MAX_REQUEST_BYTES


def test_oversized_json_is_413_and_writes_nothing(
    client: TestClient, capsules: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads((FIXTURES / "genai-traces-valid.json").read_text())
    body = json.dumps(payload).encode()
    monkeypatch.setattr(genai_ingest, "MAX_OTLP_TRACES_BYTES", len(body) - 1)
    resp = client.post(
        f"/api/otlp/v1/traces?token={TOKEN}",
        content=body,
        headers={**HEADERS, "content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.text
    assert list(capsules.iterdir()) == []


def test_body_at_the_cap_is_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads((FIXTURES / "genai-traces-valid.json").read_text())
    body = json.dumps(payload).encode()
    monkeypatch.setattr(genai_ingest, "MAX_OTLP_TRACES_BYTES", len(body))
    resp = client.post(
        f"/api/otlp/v1/traces?token={TOKEN}",
        content=body,
        headers={**HEADERS, "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text


def test_oversized_protobuf_without_content_length_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(genai_ingest, "MAX_OTLP_TRACES_BYTES", 64)

    def chunks() -> Any:
        for _ in range(10):
            yield b"\x00" * 32

    resp = client.post(
        f"/api/otlp/v1/traces?token={TOKEN}",
        content=chunks(),
        headers={**HEADERS, "content-type": "application/x-protobuf"},
    )
    assert resp.status_code == 413, resp.text


class _StreamingRequest:
    """Minimal stand-in: a Content-Length-less request body delivered in chunks."""

    def __init__(self, n_chunks: int, size: int) -> None:
        self.headers: dict[str, str] = {}
        self.consumed = 0
        self._n, self._size = n_chunks, size

    async def stream(self) -> Any:
        for _ in range(self._n):
            self.consumed += 1
            yield b"x" * self._size


def test_reader_stops_consuming_once_over_the_cap() -> None:
    req = _StreamingRequest(n_chunks=1000, size=100)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_body_capped(req, 250))  # type: ignore[arg-type]
    assert exc.value.status_code == 413
    assert req.consumed <= 3  # never the whole 100 kB body


def test_declared_content_length_over_the_cap_is_refused_before_reading() -> None:
    req = _StreamingRequest(n_chunks=1, size=10)
    req.headers["content-length"] = "1000"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_body_capped(req, 100))  # type: ignore[arg-type]
    assert exc.value.status_code == 413
    assert req.consumed == 0
