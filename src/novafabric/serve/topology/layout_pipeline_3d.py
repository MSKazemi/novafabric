"""TV-5 server-side layout pipeline using networkx spring_layout."""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Any

import networkx as nx  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

logger = logging.getLogger(__name__)

# Prometheus metrics — gracefully absent when prometheus_client is not installed.
try:
    from prometheus_client import Counter, Histogram

    layout_duration_histogram = Histogram(
        "novafabric_layout_duration_seconds",
        "TV5 layout computation duration in seconds",
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
    )
    layout_run_counter = Counter(
        "novafabric_layout_run_total",
        "Total number of TV5 layout pipeline runs",
    )
except ImportError:  # pragma: no cover
    layout_duration_histogram = None  # type: ignore[assignment]
    layout_run_counter = None  # type: ignore[assignment]


def _compute_layout_3d(
    edges: list[tuple[str, str]], node_types: dict[str, str]
) -> dict[str, list[float]]:
    """Compute 3D positions using spring_layout (FA2 approximation).

    Note: networkx spring_layout produces 2D by default.
    We run two 2D spring layouts and use the second as Z coordinate.
    """
    G: nx.DiGraph = nx.DiGraph()
    G.add_edges_from(edges)

    if len(G) == 0:
        return {}

    # 2D XY positions
    pos_xy: dict[str, Any] = nx.spring_layout(
        G, seed=42, k=2.0 / max(1, len(G) ** 0.5)
    )
    # Second layout pass for Z variation (different seed)
    pos_z: dict[str, Any] = nx.spring_layout(
        G, seed=99, k=2.0 / max(1, len(G) ** 0.5)
    )

    positions: dict[str, list[float]] = {}
    for node in G.nodes():
        x, y = pos_xy.get(node, [0.0, 0.0])
        z = pos_z.get(node, [0.0, 0.0])[1]  # Use Y from second layout as Z
        positions[str(node)] = [float(x) * 100, float(y) * 100, float(z) * 50]

    return positions


class LayoutPipeline3D:
    """Computes 3D force-directed layout for TV-5 topology snapshots."""

    def __init__(self, snapshot_store: "SnapshotStore3D") -> None:
        self._store = snapshot_store
        self._executor: ProcessPoolExecutor = ProcessPoolExecutor(max_workers=1)
        self._latest_window_id: str | None = None
        self._callbacks: list[Any] = []

    def add_snapshot_callback(self, callback: Any) -> None:
        self._callbacks.append(callback)

    def close(self) -> None:
        """Shut down the layout process pool.

        Called from the serve app lifespan finally so a topology-enabled app
        does not leak its ``ProcessPoolExecutor`` worker (and its manager
        threads) for the life of the interpreter. Idempotent and safe to call
        even if the pool already shut down.
        """
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass

    async def compute_snapshot(
        self,
        edges: list[tuple[str, str]],
        node_types: dict[str, str],
        window_id: str,
    ) -> dict[str, Any]:
        """Run layout computation in process pool (non-blocking)."""
        if layout_run_counter is not None:
            layout_run_counter.inc()

        loop = asyncio.get_event_loop()
        t0 = time.monotonic()
        try:
            positions = await loop.run_in_executor(
                self._executor,
                _compute_layout_3d,
                edges,
                node_types,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Layout computation failed: {e}")
            return {}

        elapsed_s = time.monotonic() - t0
        elapsed_ms = elapsed_s * 1000
        if layout_duration_histogram is not None:
            layout_duration_histogram.observe(elapsed_s)
        logger.info(f"TV-5 layout: {len(positions)} nodes in {elapsed_ms:.0f}ms")

        snapshot: dict[str, Any] = {
            "window_id": window_id,
            "timestamp": int(time.time()),
            "node_count": len(positions),
            "edge_count": len(edges),
            "positions": positions,
            "node_types": node_types,
            "edges": [[src, dst] for src, dst in edges],
            "layout_elapsed_ms": elapsed_ms,
        }

        self._store.save(window_id, snapshot)
        self._latest_window_id = window_id

        for cb in self._callbacks:
            try:
                await cb(window_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Snapshot callback error: {e}")

        return snapshot

    def get_latest_window_id(self) -> str | None:
        return self._latest_window_id
