/**
 * Cluster table helpers — pure sort/filter logic for the Table view.
 *
 * Kept separate from the React component so it can be unit-tested in node
 * without a DOM.
 */

import type { ClusterRow } from "./treemap.js";

export type SortKey = "cluster_id" | "agent_count" | "inter_cluster_edges";
export type SortDir = "asc" | "desc";

/** Return a new array sorted by `key` in `dir`. Stable on ties (by cluster_id asc). */
export function sortClusters(
  rows: ClusterRow[],
  key: SortKey,
  dir: SortDir,
): ClusterRow[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const d = (a[key] - b[key]) * sign;
    return d !== 0 ? d : a.cluster_id - b.cluster_id;
  });
}

/**
 * Filter rows by a free-text query. Matches against the cluster id (with or
 * without a leading "cluster " prefix). Empty query returns all rows.
 */
export function filterClusters(rows: ClusterRow[], query: string): ClusterRow[] {
  const q = query.trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((r) => {
    const haystack = `cluster ${r.cluster_id}`.toLowerCase();
    return haystack.includes(q) || String(r.cluster_id) === q;
  });
}
