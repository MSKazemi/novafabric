/**
 * IncrementalFA2Settler — Web Worker for settling new nodes in the graph.
 *
 * OQ-02 resolved: graphology-layout-forceatlas2 uses `{ fixed: true }` node
 * attribute for pinning (NOT the D3-force {fx, fy} convention).
 *
 * Protocol:
 *   receive: { type: "settle", newNodeIds: string[], graphSnapshot: SerializedGraph }
 *   send:    { type: "settled", positions: Record<string, { x: number; y: number }> }
 *            | { type: "error", message: string }
 */

import Graph from "graphology";
import FA2Layout from "graphology-layout-forceatlas2";
import type { SerializedGraph } from "graphology-types";

interface SettleRequest {
  type: "settle";
  newNodeIds: string[];
  graphSnapshot: SerializedGraph;
}

interface SettledResponse {
  type: "settled";
  positions: Record<string, { x: number; y: number }>;
}

interface ErrorResponse {
  type: "error";
  message: string;
}

self.onmessage = (ev: MessageEvent<SettleRequest>) => {
  const { type, newNodeIds, graphSnapshot } = ev.data;
  if (type !== "settle") return;

  try {
    const graph = Graph.from(graphSnapshot);
    const newSet = new Set(newNodeIds);

    // Pin all nodes NOT in newNodeIds using the fixed attribute (OQ-02).
    graph.forEachNode((nodeId) => {
      if (!newSet.has(nodeId)) {
        graph.setNodeAttribute(nodeId, "fixed", true);
      } else {
        graph.setNodeAttribute(nodeId, "fixed", false);
      }
    });

    // Run FA2 for up to 100 iterations.
    FA2Layout.assign(graph, {
      iterations: 100,
      settings: {
        gravity: 1.0,
        scalingRatio: 10.0,
        linLogMode: true,
        adjustSizes: false,
        barnesHutOptimize: false,
      },
    });

    const positions: Record<string, { x: number; y: number }> = {};
    graph.forEachNode((nodeId) => {
      positions[nodeId] = {
        x: graph.getNodeAttribute(nodeId, "x") as number,
        y: graph.getNodeAttribute(nodeId, "y") as number,
      };
    });

    const response: SettledResponse = { type: "settled", positions };
    self.postMessage(response);
  } catch (err) {
    const response: ErrorResponse = {
      type: "error",
      message: err instanceof Error ? err.message : String(err),
    };
    self.postMessage(response);
  }
};
