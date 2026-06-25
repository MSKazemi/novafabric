/**
 * Layered (Sugiyama) layout via dagre — pure function for the Call-graph view.
 *
 * Produces top-to-bottom layered positions so the directed call structure
 * (run → model → tool) reads as a flow instead of a force-directed hairball.
 * Headless and unit-testable in node (dagre has no DOM dependency).
 */

import dagre from "@dagrejs/dagre";

export interface LayoutNode {
  id: string;
  /** node size hint (Sigma node `size`); defaults to a sensible constant */
  size?: number;
}

export interface LayoutEdge {
  source: string;
  target: string;
}

export interface LayeredLayoutOptions {
  rankdir?: "TB" | "BT" | "LR" | "RL";
  nodesep?: number;
  ranksep?: number;
}

/**
 * Compute layered positions. Returns a Map of node id → {x, y} in Sigma's
 * coordinate space. Isolated nodes (no edges) are still placed by dagre.
 */
export function computeLayeredLayout(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  opts: LayeredLayoutOptions = {},
): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  if (nodes.length === 0) return positions;

  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: opts.rankdir ?? "TB",
    nodesep: opts.nodesep ?? 40,
    ranksep: opts.ranksep ?? 80,
  });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of nodes) {
    const d = Math.max(8, n.size ?? 16);
    g.setNode(n.id, { width: d * 2, height: d * 2 });
  }
  for (const e of edges) {
    if (g.hasNode(e.source) && g.hasNode(e.target)) {
      g.setEdge(e.source, e.target);
    }
  }

  dagre.layout(g);

  // dagre y grows downward; flip so the flow reads top→bottom in Sigma
  // (Sigma's y axis points up). Negating y keeps "run" sources visually on top.
  for (const id of g.nodes()) {
    const node = g.node(id);
    if (node) positions.set(id, { x: node.x, y: -node.y });
  }
  return positions;
}
