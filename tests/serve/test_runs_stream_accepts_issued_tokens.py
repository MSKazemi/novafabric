"""``GET /api/runs/stream`` authenticates like every other route (review serve-14).

The SSE route compared the token against the session token only, so a token minted
by ``POST /api/admin/tokens`` (ADR-0252) — accepted everywhere else — got 401 on the
live run stream.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "runs-stream-session-token"
LOCAL = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    store = tmp_path / "runs"
    store.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r.db"))


def _status(client: TestClient, bearer: str) -> int:
    """The response status of the stream route, without consuming its endless body.

    TestClient cannot close an infinite SSE generator cleanly, so the route's
    endpoint is awaited directly: authentication decides before any byte streams.
    """
    import asyncio

    route = next(r for r in client.app.routes if getattr(r, "path", "") == "/api/runs/stream")
    response = asyncio.run(route.endpoint(t=None, authorization=f"Bearer {bearer}"))
    return int(response.status_code)


def test_an_issued_token_opens_the_stream(client: TestClient) -> None:
    minted = client.post(
        "/api/admin/tokens", headers={**LOCAL, "authorization": f"Bearer {TOKEN}"},
        json={"label": "dashboard-viewer", "confirmed": True},
    ).json()
    assert minted["ok"] is True, minted
    assert _status(client, minted["token"]) == 200


def test_an_unknown_token_is_401(client: TestClient) -> None:
    assert _status(client, "not-a-token") == 401
