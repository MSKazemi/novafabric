/**
 * TV5Panel — 3D Three.js topology view using react-three-fiber.
 * Activated when nova serve is started with --tv5 flag.
 */
import React, { useEffect, useMemo, useState } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Text } from "@react-three/drei";
import { TimeSlider } from "./TimeSlider.js";
import { useTV5Store } from "./tv5Store.js";

/** Health color encoding: p99 latency → hue */
export function latencyToColor(p99Ms: number): string {
  if (p99Ms <= 50) return "#22c55e"; // green
  if (p99Ms <= 200) return "#eab308"; // yellow
  return "#ef4444"; // red
}

export interface NodeData {
  id: string;
  type: "agent" | "model" | "tool" | "compute_node";
  position: [number, number, number];
  p99LatencyMs?: number;
  errorRate?: number;
  label?: string;
}

export interface EdgeData {
  srcIdx: number;
  dstIdx: number;
}

interface TV5SceneProps {
  nodes: NodeData[];
  edges: EdgeData[];
  selectedNodeId: string | null;
  onNodeClick?: (nodeId: string) => void;
}

const NODE_COLORS: Record<string, string> = {
  run: "#38bdf8",
  agent: "#6366f1",
  model: "#8b5cf6",
  tool: "#f59e0b",
  compute_node: "#10b981",
};

function nodeColor(node: NodeData): string {
  if (node.p99LatencyMs !== undefined) return latencyToColor(node.p99LatencyMs);
  return NODE_COLORS[node.type] ?? "#60a5fa";
}

/** Single node sphere; label only shown when hovered or selected (declutter). */
function NodeMesh({
  node,
  showLabel,
  onNodeClick,
  onHover,
}: {
  node: NodeData;
  showLabel: boolean;
  onNodeClick?: (id: string) => void;
  onHover: (id: string | null) => void;
}) {
  const color = nodeColor(node);
  const shortLabel = (node.label ?? node.id).split("/").pop() ?? node.id;
  return (
    <group position={node.position}>
      <mesh
        onClick={(e) => {
          e.stopPropagation();
          onNodeClick?.(node.id);
        }}
        onPointerOver={(e) => {
          e.stopPropagation();
          onHover(node.id);
        }}
        onPointerOut={() => onHover(null)}
      >
        <sphereGeometry args={[5, 16, 16]} />
        <meshBasicMaterial color={color} />
      </mesh>
      {showLabel && (
        <Text
          position={[0, 8, 0]}
          fontSize={4}
          color="#e2e8f0"
          anchorX="center"
          anchorY="bottom"
          renderOrder={1}
        >
          {shortLabel}
        </Text>
      )}
    </group>
  );
}

/** Renders all nodes; labels are decluttered — only the hovered or selected
 *  node shows its label, instead of all N labels at once. */
function NodeMeshes({
  nodes,
  selectedNodeId,
  onNodeClick,
}: {
  nodes: NodeData[];
  selectedNodeId: string | null;
  onNodeClick?: (id: string) => void;
}) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  return (
    <>
      {nodes.map((node) => (
        <NodeMesh
          key={node.id}
          node={node}
          showLabel={node.id === hoveredId || node.id === selectedNodeId}
          onNodeClick={onNodeClick}
          onHover={setHoveredId}
        />
      ))}
    </>
  );
}

/** Renders all edges as a single LineSegments geometry. */
function EdgeLines({ nodes, edges }: { nodes: NodeData[]; edges: EdgeData[] }) {
  const points = useMemo(() => {
    const pts: number[] = [];
    for (const e of edges) {
      const src = nodes[e.srcIdx];
      const dst = nodes[e.dstIdx];
      if (src && dst) {
        pts.push(...src.position, ...dst.position);
      }
    }
    return new Float32Array(pts);
  }, [nodes, edges]);

  return (
    <lineSegments>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[points, 3]} />
      </bufferGeometry>
      <lineBasicMaterial color="#60a5fa" transparent opacity={0.6} />
    </lineSegments>
  );
}

/** Main TV-5 3D scene. */
function TV5Scene({ nodes, edges, selectedNodeId, onNodeClick }: TV5SceneProps) {
  return (
    <>
      <NodeMeshes nodes={nodes} selectedNodeId={selectedNodeId} onNodeClick={onNodeClick} />
      <EdgeLines nodes={nodes} edges={edges} />
      <OrbitControls makeDefault />
    </>
  );
}

interface SnapshotWindow {
  windowId: string;
  timestamp: number;
  tier: string;
}

/** TV-5 panel with time-slider and 3D canvas. */
export function TV5Panel() {
  const [nodes, setNodes] = useState<NodeData[]>([]);
  const [edges, setEdges] = useState<EdgeData[]>([]);
  const [windows, setWindows] = useState<SnapshotWindow[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Cross-panel state from zustand store
  const selectedNodeId = useTV5Store((s) => s.selectedNodeId);
  const setSelectedNode = useTV5Store((s) => s.setSelectedNode);
  const selectedWindowId = useTV5Store((s) => s.selectedWindowId);
  const setSelectedWindow = useTV5Store((s) => s.setSelectedWindow);

  const loadSnapshot = async (windowId: string) => {
    try {
      // Prefer msgpack if available; fall back to JSON
      const resp = await fetch(`/api/tv5/snapshot/${windowId}`, {
        headers: { Accept: "application/msgpack, application/json;q=0.9" },
      });
      if (!resp.ok) return;
      const contentType = resp.headers.get("content-type") ?? "";
      let snapshot: Record<string, unknown>;
      if (contentType.includes("msgpack")) {
        const buffer = await resp.arrayBuffer();
        try {
          const { decode } = await import("@msgpack/msgpack");
          snapshot = decode(new Uint8Array(buffer)) as Record<string, unknown>;
        } catch {
          // @msgpack/msgpack not bundled — fall back to JSON text decode
          snapshot = JSON.parse(new TextDecoder().decode(buffer)) as Record<
            string,
            unknown
          >;
        }
      } else {
        snapshot = (await resp.json()) as Record<string, unknown>;
      }
      applySnapshot(snapshot);
    } catch (e) {
      setError(`Failed to load snapshot ${windowId}: ${String(e)}`);
    }
  };

  const applySnapshot = (snapshot: Record<string, unknown>) => {
    const positions = (snapshot.positions ?? {}) as Record<string, number[]>;
    const nodeTypes = (snapshot.node_types ?? {}) as Record<string, string>;
    const newNodes: NodeData[] = Object.entries(positions).map(([id, pos]) => ({
      id,
      type: (nodeTypes[id] as NodeData["type"]) ?? "agent",
      position: pos as [number, number, number],
      label: id,
    }));
    setNodes(newNodes);

    // Build an index from node ID to position in the newNodes array
    const nodeIndex: Record<string, number> = {};
    newNodes.forEach((n, i) => {
      nodeIndex[n.id] = i;
    });

    // Parse edges from snapshot: [[src_id, dst_id], ...]
    const rawEdges = (snapshot.edges ?? []) as [string, string][];
    const newEdges: EdgeData[] = rawEdges
      .map(([src, dst]) => ({
        srcIdx: nodeIndex[src] ?? -1,
        dstIdx: nodeIndex[dst] ?? -1,
      }))
      .filter((e) => e.srcIdx !== -1 && e.dstIdx !== -1);
    setEdges(newEdges);
  };

  // Fetch available windows on mount
  useEffect(() => {
    fetch("/api/tv5/windows")
      .then((r) => {
        if (r.status === 404) {
          throw new Error("TV-5 router not mounted — restart the server (--topology implies --tv5 in v0.42+)");
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((ws: unknown) => {
        const windowList = Array.isArray(ws) ? (ws as SnapshotWindow[]) : [];
        setWindows(windowList);
        if (windowList.length > 0 && windowList[0]) {
          void loadSnapshot(windowList[0].windowId);
        }
      })
      .catch((e: unknown) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // WebSocket connection for live updates
  useEffect(() => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${location.host}/api/tv5/ws`);
    ws.onopen = () => {
      setWsConnected(true);
      ws.send(JSON.stringify({ type: "subscribe", topologyId: "default" }));
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string) as {
          type: string;
          windowId?: string;
        };
        if (msg.type === "snapshot" && msg.windowId) {
          // Only auto-load if we are in live mode (no window pinned)
          if (selectedWindowId === null) {
            void loadSnapshot(msg.windowId);
          }
        }
      } catch {
        // ignore malformed messages
      }
    };
    ws.onclose = () => setWsConnected(false);
    return () => {
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedWindowId]);

  // Handle window selection from the TimeSlider
  const handleWindowSelect = (windowId: string) => {
    setSelectedWindow(windowId);
    void loadSnapshot(windowId);
  };

  // Resume live mode: clear pinned window so WS messages auto-advance
  const handleLiveModeResume = () => {
    setSelectedWindow(null);
  };

  // Split windows by tier for TimeSlider
  const fineWindows = windows.filter((w) => w.tier === "fine");
  const coarseWindows = windows.filter((w) => w.tier === "coarse");

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0f172a",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "8px 16px",
          background: "#1e293b",
          display: "flex",
          alignItems: "center",
          gap: "12px",
          borderBottom: "1px solid #334155",
          flexShrink: 0,
        }}
      >
        <span style={{ color: "#94a3b8", fontSize: "12px" }}>
          TV-5 3D View {wsConnected ? "●" : "○"}
        </span>
        {selectedNodeId && (
          <span style={{ color: "#60a5fa", fontSize: "12px" }}>
            Selected: {selectedNodeId}
          </span>
        )}
      </div>

      {/* 3D Canvas */}
      <div style={{ flex: 1, position: "relative", minHeight: 0 }}>
        {error && (
          <div
            style={{
              position: "absolute",
              top: 8,
              right: 8,
              background: "#7f1d1d",
              color: "#fca5a5",
              padding: "4px 8px",
              borderRadius: 4,
              fontSize: 12,
              zIndex: 10,
            }}
          >
            {error}
          </div>
        )}
        {nodes.length === 0 ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              color: "#9ca3af",
            }}
          >
            {error
              ? null
              : "No snapshot data yet — trigger a run or call /api/topology/seed"}
          </div>
        ) : (
          <Canvas
            camera={{ position: [0, 0, 200], fov: 60 }}
            onCreated={({ gl }) => {
              gl.setClearColor("#0f172a", 1);
              gl.domElement.addEventListener("webglcontextlost", () => {
                setError("WebGL context lost — refresh to reconnect");
              });
            }}
          >
            <TV5Scene
              nodes={nodes}
              edges={edges}
              selectedNodeId={selectedNodeId}
              onNodeClick={setSelectedNode}
            />
          </Canvas>
        )}
      </div>

      {/* Animated TimeSlider — only shown when there are snapshot windows */}
      {(fineWindows.length > 0 || coarseWindows.length > 0) && (
        <TimeSlider
          fineWindows={fineWindows}
          coarseWindows={coarseWindows}
          selectedWindowId={selectedWindowId}
          isLive={wsConnected && selectedWindowId === null}
          onWindowSelect={handleWindowSelect}
          onLiveModeResume={handleLiveModeResume}
        />
      )}
    </div>
  );
}
