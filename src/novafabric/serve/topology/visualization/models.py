"""TV-5 3D Topology Pydantic models.

Canonical data contracts for topology snapshots exchanged between the
layout pipeline, snapshot store, REST/WebSocket router, and the browser SPA.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NodeRecord(BaseModel):
    """A single node in the 3D topology graph."""

    node_id: str = Field(..., description="Unique node identifier")
    node_type: Literal["agent", "model", "tool", "compute"] = Field(
        ..., description="Functional category of the node"
    )
    label: str = Field(..., description="Human-readable display label")
    cluster_id: str = Field(..., description="ID of the owning cluster")
    health: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Health score 0.0 (critical) … 1.0 (healthy)",
    )
    x: float = Field(default=0.0, description="X position in 3D space")
    y: float = Field(default=0.0, description="Y position in 3D space")
    z: float = Field(default=0.0, description="Z position in 3D space")


class ClusterRecord(BaseModel):
    """A logical cluster grouping several nodes."""

    cluster_id: str = Field(..., description="Unique cluster identifier")
    label: str = Field(..., description="Human-readable cluster label")
    centroid: tuple[float, float, float] = Field(
        default=(0.0, 0.0, 0.0),
        description="3D centroid of the cluster (x, y, z)",
    )
    node_count: int = Field(default=0, ge=0, description="Number of member nodes")


class WindowRecord(BaseModel):
    """Metadata for a single snapshot time window."""

    window_id: str = Field(..., description="Unique window identifier (URL-safe)")
    tier: Literal["fine", "coarse"] = Field(
        ..., description="'fine' = 5-min intervals; 'coarse' = 1-hour intervals"
    )
    captured_at: float = Field(..., description="Unix timestamp when snapshot was captured")


class TopologySnapshot(BaseModel):
    """Complete 3D topology snapshot — nodes, clusters, and metadata."""

    window_id: str = Field(..., description="Unique window identifier")
    tier: Literal["fine", "coarse"] = Field(default="fine")
    captured_at: float = Field(..., description="Unix timestamp of capture")
    nodes: list[NodeRecord] = Field(default_factory=list)
    clusters: list[ClusterRecord] = Field(default_factory=list)

    @classmethod
    def from_raw(cls, data: dict[str, object]) -> "TopologySnapshot":
        """Build a TopologySnapshot from the raw dict produced by LayoutPipeline3D.

        The raw dict uses positions/node_types/edges keys rather than the typed
        NodeRecord/ClusterRecord structure.  This factory provides a compatibility
        shim so existing raw snapshots can be surfaced through the typed API.
        """
        import time as _time

        raw_positions = data.get("positions") or {}
        raw_node_types = data.get("node_types") or {}
        positions: dict[str, list[float]] = raw_positions  # type: ignore[assignment]
        node_types: dict[str, str] = raw_node_types  # type: ignore[assignment]

        nodes: list[NodeRecord] = []
        for node_id, pos in positions.items():
            raw_type = node_types.get(node_id, "agent")
            # Map "compute_node" → "compute" to match our literal set
            if raw_type == "compute_node":
                mapped_type: Literal["agent", "model", "tool", "compute"] = "compute"
            elif raw_type in ("agent", "model", "tool", "compute"):
                mapped_type = raw_type  # type: ignore[assignment]
            else:
                mapped_type = "agent"
            nodes.append(
                NodeRecord(
                    node_id=node_id,
                    node_type=mapped_type,
                    label=node_id,
                    cluster_id="default",
                    x=float(pos[0]) if len(pos) > 0 else 0.0,
                    y=float(pos[1]) if len(pos) > 1 else 0.0,
                    z=float(pos[2]) if len(pos) > 2 else 0.0,
                )
            )

        raw_ts = data.get("timestamp")
        captured_at: float = float(raw_ts) if raw_ts is not None else _time.time()  # type: ignore[arg-type]
        return cls(
            window_id=str(data.get("window_id", "")),
            tier="fine",
            captured_at=captured_at,
            nodes=nodes,
            clusters=[],
        )
