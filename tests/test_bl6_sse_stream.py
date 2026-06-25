"""Tests for BL-6: SSE stream endpoint at /api/runs/stream.

Verifies that:
- The route /api/runs/stream exists in the FastAPI app
- The route is a GET endpoint
- A request with invalid token returns 401 with text/event-stream content-type
  (the 401 branch returns iter([]) which does not block)
- The _RunEventBus subscribe/unsubscribe/publish methods are correct
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import _RunEventBus, create_app  # noqa: E402

TOKEN = "test-token-sse-bl6"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


def _make_client(tmp_path: Path) -> TestClient:
    app = create_app(token=TOKEN, capsule_dir=tmp_path / ".novafabric" / "runs", db_path=None)
    return TestClient(app)


def test_sse_endpoint_returns_event_stream_for_invalid_token(tmp_path: Path) -> None:
    """GET /api/runs/stream with wrong token must return 401 text/event-stream.

    The 401 branch returns StreamingResponse(iter([]), ...) which terminates
    immediately — safe to call with a regular TestClient.
    """
    client = _make_client(tmp_path)
    resp = client.get(
        "/api/runs/stream",
        params={"token": "wrong-token"},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 401
    ct = resp.headers.get("content-type", "")
    assert "text/event-stream" in ct


def test_sse_endpoint_returns_event_stream_for_missing_token(tmp_path: Path) -> None:
    """GET /api/runs/stream without a token must return 401 text/event-stream."""
    client = _make_client(tmp_path)
    resp = client.get(
        "/api/runs/stream",
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 401
    ct = resp.headers.get("content-type", "")
    assert "text/event-stream" in ct


def test_sse_route_exists_in_app(tmp_path: Path) -> None:
    """The /api/runs/stream GET route must be registered in the FastAPI app."""
    app = create_app(token=TOKEN, capsule_dir=tmp_path / ".novafabric" / "runs", db_path=None)
    route_paths = [r.path for r in app.routes if hasattr(r, "path")]  # type: ignore[union-attr]
    assert "/api/runs/stream" in route_paths, (
        f"Route /api/runs/stream not found; registered paths: {route_paths}"
    )


# ---------------------------------------------------------------------------
# _RunEventBus unit tests — no HTTP needed
# ---------------------------------------------------------------------------


def test_run_event_bus_subscribe_and_unsubscribe() -> None:
    """Subscribing returns a queue; unsubscribing removes it."""
    bus = _RunEventBus()
    q = bus.subscribe()
    assert q in bus._queues
    bus.unsubscribe(q)
    assert q not in bus._queues


def test_run_event_bus_publish_delivers_to_subscriber() -> None:
    """A published run must appear in the subscriber's queue."""
    bus = _RunEventBus()
    q = bus.subscribe()
    run = {"run_id": "abc123", "status": "success"}
    bus.publish(run)
    received = q.get_nowait()
    assert received == run
    bus.unsubscribe(q)


def test_run_event_bus_unsubscribe_nonexistent_is_noop() -> None:
    """Unsubscribing a queue that was never subscribed must not raise."""
    bus = _RunEventBus()
    orphan: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
    bus.unsubscribe(orphan)  # must not raise


def test_run_event_bus_publish_drops_when_queue_full() -> None:
    """Publish to a full queue must not block or raise — it silently drops."""
    bus = _RunEventBus()
    q = bus.subscribe()
    # Fill the queue to capacity (maxsize=64)
    for i in range(q.maxsize):
        q.put_nowait({"run_id": str(i)})
    # This must not raise even though the queue is full
    bus.publish({"run_id": "overflow"})
    bus.unsubscribe(q)
