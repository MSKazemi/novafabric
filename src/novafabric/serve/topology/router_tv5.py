"""TV-5 3D Topology REST + WebSocket API."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

if TYPE_CHECKING:
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

logger = logging.getLogger(__name__)
WINDOW_ID_RE = re.compile(r"^[a-z0-9_-]+$")


def make_tv5_router(
    snapshot_store: "SnapshotStore3D",
    layout_pipeline: "LayoutPipeline3D",
    *,
    start_retention_loop: bool = True,
) -> APIRouter:
    router = APIRouter(prefix="/api/tv5", tags=["tv5"])

    # Track connected WebSocket clients
    _ws_clients: list[WebSocket] = []

    # Start TTL-based retention loop as a background task
    if start_retention_loop:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(snapshot_store.start_retention_loop())
        except RuntimeError:
            # No running event loop yet (e.g. during tests); skip.
            pass

    async def _broadcast_new_snapshot(window_id: str) -> None:
        msg = json.dumps({"type": "snapshot", "windowId": window_id})
        disconnected: list[WebSocket] = []
        for ws in _ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                disconnected.append(ws)
        for ws in disconnected:
            _ws_clients.remove(ws)

    layout_pipeline.add_snapshot_callback(_broadcast_new_snapshot)

    @router.get("/live")
    async def get_live_info() -> dict[str, Any]:
        """Return current topology info."""
        window_id = layout_pipeline.get_latest_window_id()
        return {
            "windowId": window_id,
            "nodeCount": 0,
            "edgeCount": 0,
            "layoutAgeMs": 0,
        }

    @router.get("/windows")
    async def list_windows(
        from_ts: int = Query(0, alias="from"),
        to_ts: int = Query(9999999999, alias="to"),
    ) -> list[dict[str, Any]]:
        """List available snapshot windows in a time range."""
        windows = snapshot_store.list_windows()
        return [w for w in windows if from_ts <= w["timestamp"] <= to_ts]

    @router.get("/snapshot/{window_id}", response_model=None)
    async def get_snapshot(window_id: str) -> Any:
        """Return topology snapshot as JSON or msgpack bytes."""
        if not WINDOW_ID_RE.match(window_id):
            raise HTTPException(400, "Invalid window_id format")
        snap = snapshot_store.get(window_id)
        if snap is None:
            raise HTTPException(404, detail={"error": "snapshot not found or expired"})
        content_type = snapshot_store.content_type
        if "msgpack" in content_type:
            import msgpack  # type: ignore[import-not-found]

            return Response(
                msgpack.packb(snap, use_bin_type=True),
                media_type="application/msgpack",
            )
        return snap

    @router.websocket("/ws")
    async def tv5_websocket(websocket: WebSocket) -> None:
        """TV-5 WebSocket: server pushes {type: snapshot, windowId} on new layouts."""
        await websocket.accept()
        _ws_clients.append(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "subscribe":
                        topology_id = msg.get("topologyId", "default")
                        await websocket.send_text(
                            json.dumps({"type": "subscribed", "topologyId": topology_id})
                        )
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in _ws_clients:
                _ws_clients.remove(websocket)

    return router
