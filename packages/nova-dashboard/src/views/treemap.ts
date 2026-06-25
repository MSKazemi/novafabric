/**
 * Treemap layout — pure function over the cluster list.
 *
 * Maps each cluster to a rectangle whose AREA is proportional to its agent
 * count, using d3-hierarchy's squarified treemap. Rendering (SVG) lives in the
 * thin React wrapper; this module is headless and unit-testable in node.
 */

import { hierarchy, treemap, treemapSquarify } from "d3-hierarchy";

export interface ClusterRow {
  cluster_id: number;
  agent_count: number;
  inter_cluster_edges: number;
}

export interface TreemapCell {
  cluster_id: number;
  agent_count: number;
  inter_cluster_edges: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/**
 * Compute treemap cells for the given clusters within a width × height box.
 * Cells are returned largest-first. Clusters with agent_count <= 0 are skipped
 * (a treemap value must be positive to occupy area).
 */
export function computeTreemap(
  clusters: ClusterRow[],
  width: number,
  height: number,
): TreemapCell[] {
  const positive = clusters.filter((c) => c.agent_count > 0);
  if (positive.length === 0 || width <= 0 || height <= 0) return [];

  const root = hierarchy<{ children: ClusterRow[] } | ClusterRow>({
    children: positive,
  })
    .sum((d) => ("agent_count" in d ? d.agent_count : 0))
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));

  const layout = treemap<{ children: ClusterRow[] } | ClusterRow>()
    .tile(treemapSquarify)
    .size([width, height])
    .padding(2)(root);

  return layout.leaves().map((leaf) => {
    const row = leaf.data as ClusterRow;
    return {
      cluster_id: row.cluster_id,
      agent_count: row.agent_count,
      inter_cluster_edges: row.inter_cluster_edges,
      x0: leaf.x0 ?? 0,
      y0: leaf.y0 ?? 0,
      x1: leaf.x1 ?? 0,
      y1: leaf.y1 ?? 0,
    };
  });
}
