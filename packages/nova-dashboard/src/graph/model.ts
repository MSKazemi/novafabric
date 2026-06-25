/**
 * GraphologyModel — in-memory graph with delta apply, expand/collapse, LOD ceiling.
 *
 * Owns a Graphology DirectedGraph. Receives TDP delta events and Arrow IPC
 * cluster-layer + subgraph batches from TDPClient.
 */

import Graph from "graphology";
import { tableFromIPC } from "apache-arrow";
import type { SerializedGraph } from "graphology-types";
import type { TdpDeltaEvent } from "../tdp/types.js";

const LOD_CEILING = 5;

// Callback type for notifying the renderer to refresh.
export type RefreshCallback = (newNodeIds?: string[]) => void;

export class GraphologyModel {
  readonly graph: Graph;
  private _expandedClusters: number[] = [];  // LRU order (oldest first)
  private _clusterCentroid: Map<number, { x: number; y: number }> = new Map();
  private _refreshCb: RefreshCallback | null = null;
  private _fa2Worker: Worker | null = null;

  constructor() {
    this.graph = new Graph({ type: "directed", multi: false });
  }

  setRefreshCallback(cb: RefreshCallback): void {
    this._refreshCb = cb;
  }

  setFA2Worker(worker: Worker): void {
    this._fa2Worker = worker;
    worker.onmessage = (ev) => this._onWorkerMessage(ev);
  }

  // -----------------------------------------------------------------------
  // Cluster layer (super-node view)
  // -----------------------------------------------------------------------

  applyClusterLayer(ipc: Uint8Array): void {
    const table = tableFromIPC(ipc);
    const clusterIds = table.getChild("cluster_id");
    const centroidXs = table.getChild("centroid_x");
    const centroidYs = table.getChild("centroid_y");
    const agentCounts = table.getChild("agent_count");
    const interEdges = table.getChild("inter_cluster_edges");

    if (!clusterIds || !centroidXs || !centroidYs || !agentCounts || !interEdges) return;

    for (let i = 0; i < table.numRows; i++) {
      const cid = Number(clusterIds.get(i));
      const x = centroidXs.get(i) as number;
      const y = centroidYs.get(i) as number;
      const nodeId = `cluster:${cid}`;

      this._clusterCentroid.set(cid, { x, y });

      const agentCount = Number(agentCounts.get(i));
      // De-emphasize singleton clusters (one agent) so meaningful clusters
      // dominate: smaller and a muted slate instead of the primary blue.
      const isSingleton = agentCount <= 1;
      const attrs = {
        type: "cluster",
        cluster_id: cid,
        x,
        y,
        size: isSingleton ? 6 : Math.max(16, Math.sqrt(agentCount) * 8),
        color: isSingleton ? "#cbd5e1" : "#3b82f6",
        label: `cluster ${cid} (${agentCount})`,
        agent_count: agentCount,
        inter_cluster_edges: Number(interEdges.get(i)),
      };

      if (!this.graph.hasNode(nodeId)) {
        this.graph.addNode(nodeId, attrs);
      } else {
        Object.entries(attrs).forEach(([k, v]) => this.graph.setNodeAttribute(nodeId, k, v));
      }
    }

    this._refreshCb?.();
  }

  applyClusterEdges(
    edges: Array<{ source_cluster: number; target_cluster: number; count: number }>,
  ): void {
    for (const e of edges) {
      const src = `cluster:${e.source_cluster}`;
      const tgt = `cluster:${e.target_cluster}`;
      if (this.graph.hasNode(src) && this.graph.hasNode(tgt)) {
        this.graph.mergeEdge(src, tgt, {
          size: Math.max(1, Math.sqrt(e.count)),
          color: "#94a3b8",
        });
      }
    }
    this._refreshCb?.();
  }

  // -----------------------------------------------------------------------
  // Delta events
  // -----------------------------------------------------------------------

  applyDeltaBatch(events: TdpDeltaEvent[]): void {
    for (const ev of events) {
      switch (ev.type) {
        case "add_node": {
          const agentColor = ev.agent_type === "model" ? "#8b5cf6" : "#6366f1";
          this.graph.mergeNode(ev.node_id, {
            type: ev.agent_type === "model" ? "model" : "agent",
            agent_type: ev.agent_type,
            status: ev.status,
            cluster_id: ev.cluster_id,
            x: ev.pos_x,
            y: ev.pos_y,
            size: 8,
            color: agentColor,
            label: ev.node_id,
          });
          break;
        }
        case "remove_node":
          if (this.graph.hasNode(ev.node_id)) this.graph.dropNode(ev.node_id);
          break;
        case "add_edge":
          if (this.graph.hasNode(ev.source_id) && this.graph.hasNode(ev.target_id)) {
            this.graph.mergeEdge(ev.source_id, ev.target_id, { edge_type: ev.edge_type, color: "#60a5fa", size: 1 });
          }
          break;
        case "remove_edge":
          if (this.graph.hasEdge(ev.source_id, ev.target_id)) {
            this.graph.dropEdge(ev.source_id, ev.target_id);
          }
          break;
        case "update_property":
          if (this.graph.hasNode(ev.node_id)) {
            this.graph.setNodeAttribute(ev.node_id, ev.key, ev.value);
          }
          break;
        case "topology_reset":
          this._handleTopologyReset();
          break;
      }
    }
    this._refreshCb?.();
  }

  // -----------------------------------------------------------------------
  // Expand / collapse
  // -----------------------------------------------------------------------

  expandCluster(
    clusterId: number,
    nodesBatch: Uint8Array,
    edgesBatch: Uint8Array,
  ): void {
    // LOD ceiling: collapse oldest if already at limit
    if (this._expandedClusters.length >= LOD_CEILING) {
      const oldest = this._expandedClusters.shift()!;
      this._collapseClusterInternal(oldest);
    }

    // Remove the cluster super-node
    const superNodeId = `cluster:${clusterId}`;
    if (this.graph.hasNode(superNodeId)) this.graph.dropNode(superNodeId);

    // Add agent nodes from Arrow IPC
    const nodeTable = tableFromIPC(nodesBatch);
    const nodeIds: string[] = [];
    const centroid = this._clusterCentroid.get(clusterId) ?? { x: 0, y: 0 };

    const nids = nodeTable.getChild("node_id");
    const types = nodeTable.getChild("agent_type");
    const statuses = nodeTable.getChild("status");
    const posXs = nodeTable.getChild("pos_x");
    const posYs = nodeTable.getChild("pos_y");

    if (nids) {
      for (let i = 0; i < nodeTable.numRows; i++) {
        const nodeId = nids.get(i) as string;
        nodeIds.push(nodeId);
        const agentType = types?.get(i) as string ?? "unknown";
        this.graph.mergeNode(nodeId, {
          type: agentType === "model" ? "model" : "agent",
          agent_type: agentType,
          status: statuses?.get(i) ?? "idle",
          cluster_id: clusterId,
          x: centroid.x + (Math.random() - 0.5) * 10,
          y: centroid.y + (Math.random() - 0.5) * 10,
          size: 8,
          color: agentType === "model" ? "#8b5cf6" : "#6366f1",
          label: nodeId,
        });
      }
    }

    // Add agent edges from Arrow IPC
    const edgeTable = tableFromIPC(edgesBatch);
    const sids = edgeTable.getChild("source_id");
    const tids = edgeTable.getChild("target_id");
    const etypes = edgeTable.getChild("edge_type");
    if (sids && tids) {
      for (let i = 0; i < edgeTable.numRows; i++) {
        const src = sids.get(i) as string;
        const tgt = tids.get(i) as string;
        if (this.graph.hasNode(src) && this.graph.hasNode(tgt)) {
          this.graph.mergeEdge(src, tgt, { edge_type: etypes?.get(i) ?? "calls" });
        }
      }
    }

    this._expandedClusters.push(clusterId);

    // Post to FA2 worker for layout settling
    if (this._fa2Worker && nodeIds.length > 0) {
      const snapshot = this.graph.toJSON() as SerializedGraph;
      this._fa2Worker.postMessage({
        type: "settle",
        newNodeIds: nodeIds,
        graphSnapshot: snapshot,
      });
    } else {
      this._refreshCb?.(nodeIds);
    }
  }

  collapseCluster(clusterId: number): void {
    const idx = this._expandedClusters.indexOf(clusterId);
    if (idx !== -1) this._expandedClusters.splice(idx, 1);
    this._collapseClusterInternal(clusterId);
  }

  // -----------------------------------------------------------------------
  // Private helpers
  // -----------------------------------------------------------------------

  private _collapseClusterInternal(clusterId: number): void {
    // Remove all agent nodes for this cluster
    const toRemove: string[] = [];
    this.graph.forEachNode((nodeId) => {
      if (this.graph.getNodeAttribute(nodeId, "cluster_id") === clusterId &&
          this.graph.getNodeAttribute(nodeId, "type") === "agent") {
        toRemove.push(nodeId);
      }
    });
    for (const n of toRemove) this.graph.dropNode(n);

    // Re-add the super-node
    const centroid = this._clusterCentroid.get(clusterId) ?? { x: 0, y: 0 };
    this.graph.mergeNode(`cluster:${clusterId}`, {
      type: "cluster",
      cluster_id: clusterId,
      x: centroid.x,
      y: centroid.y,
      size: 16,
      color: "#3b82f6",
    });

    this._refreshCb?.();
  }

  private _handleTopologyReset(): void {
    this.graph.clear();
    this._expandedClusters = [];
    this._clusterCentroid.clear();
  }

  private _onWorkerMessage(ev: MessageEvent): void {
    const data = ev.data as { type: string; positions?: Record<string, { x: number; y: number }> };
    if (data.type === "settled" && data.positions) {
      for (const [nodeId, pos] of Object.entries(data.positions)) {
        if (this.graph.hasNode(nodeId)) {
          this.graph.setNodeAttribute(nodeId, "x", pos.x);
          this.graph.setNodeAttribute(nodeId, "y", pos.y);
        }
      }
    }
    this._refreshCb?.();
  }
}
