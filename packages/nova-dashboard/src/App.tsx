/**
 * Nova Dashboard SPA — root React component.
 *
 * Wires up TDPClient → GraphologyModel → SigmaRenderer on mount and offers a
 * view-mode switcher over five complementary lenses on the same topology:
 *
 *   cluster   — 2D force layout, cluster super-nodes, click-to-expand (default)
 *   callgraph — 2D layered (dagre) layout showing run → model → tool flow
 *   treemap   — cluster sizes at a glance (area ∝ agent count)
 *   table     — sortable / searchable exact list
 *   tv5       — experimental 3D view (Three.js); kept, not default
 */

import { useEffect, useRef, useState } from "react";
import { TDPClient } from "./tdp/client.js";
import { GraphologyModel } from "./graph/model.js";
import { SigmaRenderer } from "./renderer/renderer.js";
import { TV5Panel } from "./components/topology/TV5Panel.js";
import { TreemapView } from "./components/topology/TreemapView.js";
import { TableView } from "./components/topology/TableView.js";
import type { TdpDeltaEvent, MetricSummaryEvent } from "./tdp/types.js";

export type ViewMode = "cluster" | "callgraph" | "treemap" | "table" | "tv5";

const VIEW_MODES: Array<{ id: ViewMode; label: string }> = [
  { id: "cluster", label: "Cluster" },
  { id: "callgraph", label: "Call-graph" },
  { id: "treemap", label: "Treemap" },
  { id: "table", label: "Table" },
  { id: "tv5", label: "3D (experimental)" },
];

// A graph mode renders into the shared Sigma container.
const GRAPH_MODES: ViewMode[] = ["cluster", "callgraph"];

// Token is injected into the page URL as ?token=...
function getToken(): string {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") ?? "";
}

function buildWsUrl(token: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/topology/stream?token=${token}`;
}

function buildSseUrl(token: string): string {
  return `${window.location.origin}/metrics/stream?token=${token}`;
}

function buildClustersUrl(token: string): string {
  return `${window.location.origin}/topology/clusters?token=${token}`;
}

function buildClusterEdgesUrl(token: string): string {
  return `${window.location.origin}/topology/cluster-edges?token=${token}`;
}

export default function App(): React.ReactElement {
  const containerRef = useRef<HTMLDivElement>(null);
  const rendererRef = useRef<SigmaRenderer | null>(null);
  const clientRef = useRef<TDPClient | null>(null);
  const [metrics, setMetrics] = useState<MetricSummaryEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<ViewMode>("cluster");
  const token = getToken();

  useEffect(() => {
    if (!token) {
      setError("Missing ?token= in URL. Start the server with nova serve --experimental --topology.");
      return;
    }

    const model = new GraphologyModel();
    const client = new TDPClient();
    clientRef.current = client;

    // FA2 worker for layout settling
    const worker = new Worker(new URL("./graph/fa2-worker.ts", import.meta.url), { type: "module" });
    model.setFA2Worker(worker);

    let renderer: SigmaRenderer | null = null;
    if (containerRef.current) {
      renderer = new SigmaRenderer(containerRef.current, model);
      rendererRef.current = renderer;
      renderer.setNodeClickHandler((clusterId) => {
        if (clusterId !== null) {
          client.sendExpandRequest(clusterId);
        }
      });
    }

    client.onTopologyDelta((ev: TdpDeltaEvent) => {
      model.applyDeltaBatch([ev]);
    });

    client.onSubgraphExpand((clusterId, nodesBatch, edgesBatch) => {
      model.expandCluster(clusterId, nodesBatch, edgesBatch);
    });

    client.onMetricFrame((ev: MetricSummaryEvent) => setMetrics(ev));

    // Fetch initial cluster layer, then inter-cluster edges
    void fetch(buildClustersUrl(token), {
      headers: { Accept: "application/vnd.apache.arrow.stream" },
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.arrayBuffer();
      })
      .then((buf) => {
        model.applyClusterLayer(new Uint8Array(buf));
        // Fetch inter-cluster edges after cluster nodes exist in graph
        return fetch(buildClusterEdgesUrl(token));
      })
      .then((r) => r.json())
      .then((edges) => {
        model.applyClusterEdges(
          edges as Array<{ source_cluster: number; target_cluster: number; count: number }>,
        );
        rendererRef.current?.fit();
      })
      .catch((e: unknown) => setError(String(e)));

    client.connect(buildWsUrl(token), buildSseUrl(token));

    return () => {
      client.disconnect();
      worker.terminate();
      renderer?.destroy();
      rendererRef.current = null;
      clientRef.current = null;
    };
  }, [token]);

  // Swap layout when moving between the two graph modes.
  useEffect(() => {
    const r = rendererRef.current;
    if (!r) return;
    if (mode === "callgraph") {
      r.applyLayeredLayout();
    } else if (mode === "cluster") {
      r.restoreForceLayout();
    }
  }, [mode]);

  // Expanding a cluster from the treemap/table jumps back to the cluster graph.
  const handleSelectCluster = (clusterId: number): void => {
    clientRef.current?.sendExpandRequest(clusterId);
    setMode("cluster");
  };

  if (error) {
    return (
      <div style={{ padding: 24, fontFamily: "monospace", color: "#e55" }}>
        <strong>Nova Dashboard error:</strong> {error}
      </div>
    );
  }

  const tabBtnStyle = (id: ViewMode): React.CSSProperties => ({
    padding: "4px 14px",
    fontSize: 12,
    fontFamily: "monospace",
    background: mode === id ? "#3b82f6" : "transparent",
    color: mode === id ? "#fff" : "#6b7280",
    border: "1px solid",
    borderColor: mode === id ? "#3b82f6" : "#e5e7eb",
    cursor: "pointer",
    borderRadius: 4,
  });

  const isGraph = GRAPH_MODES.includes(mode);

  return (
    <div style={{ width: "100vw", height: "100vh", display: "flex", flexDirection: "column" }}>
      {/* Top bar: metrics + view-mode switcher */}
      <div style={{
        padding: "4px 12px",
        background: "#ffffff",
        borderBottom: "1px solid #e5e7eb",
        color: "#374151",
        fontSize: 12,
        fontFamily: "monospace",
        display: "flex",
        gap: 24,
        alignItems: "center",
      }}>
        {metrics && (
          <>
            <span>nodes: {metrics.node_count}</span>
            <span>edges: {metrics.edge_count}</span>
            <span>clusters: {metrics.cluster_count}</span>
          </>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          {VIEW_MODES.map((m) => (
            <button key={m.id} style={tabBtnStyle(m.id)} onClick={() => setMode(m.id)}>
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {/* Main view area */}
      <div style={{ flex: 1, position: "relative", minHeight: 0 }}>
        {/* Shared Sigma container — used by cluster + callgraph modes */}
        <div
          ref={containerRef}
          style={{
            position: "absolute",
            inset: 0,
            background: "#f8fafc",
            display: isGraph ? "block" : "none",
          }}
          aria-label="Agent topology graph"
        />

        {/* Zoom controls (graph modes only) */}
        {isGraph && (
          <div style={{
            position: "absolute",
            right: 12,
            bottom: 12,
            display: "flex",
            flexDirection: "column",
            gap: 4,
            zIndex: 10,
          }}>
            <ZoomButton label="+" title="Zoom in" onClick={() => rendererRef.current?.zoomIn()} />
            <ZoomButton label="−" title="Zoom out" onClick={() => rendererRef.current?.zoomOut()} />
            <ZoomButton label="⤢" title="Fit to screen" onClick={() => rendererRef.current?.fit()} />
          </div>
        )}

        {mode === "treemap" && (
          <TreemapView token={token} onSelectCluster={handleSelectCluster} />
        )}
        {mode === "table" && (
          <TableView token={token} onSelectCluster={handleSelectCluster} />
        )}
        {mode === "tv5" && (
          <div style={{ position: "absolute", inset: 0, background: "#111827" }}>
            <TV5Panel />
          </div>
        )}
      </div>
    </div>
  );
}

function ZoomButton(props: { label: string; title: string; onClick: () => void }): React.ReactElement {
  return (
    <button
      title={props.title}
      onClick={props.onClick}
      style={{
        width: 32,
        height: 32,
        fontSize: 16,
        fontFamily: "monospace",
        background: "#ffffff",
        color: "#374151",
        border: "1px solid #e5e7eb",
        borderRadius: 4,
        cursor: "pointer",
        boxShadow: "0 1px 2px rgba(0,0,0,0.08)",
      }}
    >
      {props.label}
    </button>
  );
}
