"""Every WebSocket route ``nova serve`` mounts rejects a missing or wrong token.

The app-level authorization dependency stands aside for WebSocket scopes
(ADR-0228 OQ-2, ``serve/authz.py``). Each WebSocket route checks the Host header
and the token inline, before ``accept()``. Nothing pinned that every mounted
route actually does so. This test enumerates the WebSocket routes on the app the
way ``nova serve --topology`` builds it (``create_app`` plus the TV-5 router
that ``cli/serve.py`` adds), so a new route without the inline check fails here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.routing import WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "ws-token-0123456789abcdefABCDEF"
HOST = {"host": "127.0.0.1:4321"}
#: Subprotocols a route may require before it even looks at the token.
SUBPROTOCOLS = ["nova-tdp-v1"]


def _build_app(tmp_path: Path) -> FastAPI:
    """Build the app as ``nova serve --topology`` does (``cli/serve.py``)."""
    from novafabric.cli.serve import _build_tv5_router

    tv5_router, _pipe = _build_tv5_router(TOKEN)
    return create_app(
        token=TOKEN,
        capsule_dir=tmp_path / "capsules",
        db_path=None,
        static_dir=None,
        topology_enabled=True,
        extra_routers=[tv5_router],
    )


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    (tmp_path / "capsules").mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    yield _build_app(tmp_path)


def _ws_routes(routes: Sequence[Any]) -> list[WebSocketRoute]:
    """WebSocket routes in matching order, descending into included routers.

    FastAPI 0.141 keeps an included router as one lazy entry whose routes live
    on ``original_router`` (already carrying the router's own prefix).
    """
    found: list[WebSocketRoute] = []
    for route in routes:
        if isinstance(route, WebSocketRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(_ws_routes(route.original_router.routes))
        elif hasattr(route, "routes") and not hasattr(route, "path_format"):
            found.extend(_ws_routes(route.routes))
    return found


def _ws_paths(app: FastAPI) -> list[str]:
    return [r.path.replace("{ws_path:path}", "anything") for r in _ws_routes(app.router.routes)]


def test_the_known_websocket_routes_are_mounted(app: FastAPI) -> None:
    paths = _ws_paths(app)
    assert "/topology/stream" in paths
    assert "/api/tv5/ws" in paths


@pytest.mark.parametrize("query", ["", "?token=", "?token=wrong-token-0123456789abcdef"])
def test_every_websocket_route_rejects_a_missing_or_wrong_token(
    app: FastAPI, query: str
) -> None:
    client = TestClient(app)
    paths = _ws_paths(app)
    assert paths
    for path in paths:
        with pytest.raises(WebSocketDisconnect) as info:
            with client.websocket_connect(path + query, headers=HOST, subprotocols=SUBPROTOCOLS):
                pass
        assert info.value.code in (1011, 4401, 4403, 4400), (path, info.value.code)


def test_the_tv5_websocket_accepts_the_right_token(app: FastAPI) -> None:
    """The TV-5 route is reachable: the WebSocket catch-all must not shadow it."""
    client = TestClient(app)
    with client.websocket_connect(f"/api/tv5/ws?token={TOKEN}", headers=HOST) as ws:
        ws.send_text('{"type": "subscribe", "topologyId": "default"}')
        assert ws.receive_json()["type"] == "subscribed"


def test_the_websocket_catch_all_stays_last(app: FastAPI) -> None:
    assert _ws_routes(app.router.routes)[-1].path == "/{ws_path:path}"
