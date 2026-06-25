/**
 * TableView — the anti-hairball. Exact, sortable, searchable cluster list.
 *
 * Reads /topology/cluster-list (plain JSON). Clicking a row asks the parent to
 * expand that cluster (and typically switch to the cluster graph view).
 */

import { useEffect, useMemo, useState } from "react";
import type { ClusterRow } from "../../views/treemap.js";
import {
  filterClusters,
  sortClusters,
  type SortDir,
  type SortKey,
} from "../../views/clusterTable.js";

interface Props {
  token: string;
  onSelectCluster: (clusterId: number) => void;
}

function buildUrl(token: string): string {
  return `${window.location.origin}/topology/cluster-list?token=${token}`;
}

const COLUMNS: Array<{ key: SortKey; label: string }> = [
  { key: "cluster_id", label: "cluster" },
  { key: "agent_count", label: "agents" },
  { key: "inter_cluster_edges", label: "inter-cluster edges" },
];

export function TableView({ token, onSelectCluster }: Props): React.ReactElement {
  const [rows, setRows] = useState<ClusterRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("agent_count");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  useEffect(() => {
    void fetch(buildUrl(token))
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => setRows(data as ClusterRow[]))
      .catch((e: unknown) => setError(String(e)));
  }, [token]);

  const view = useMemo(
    () => sortClusters(filterClusters(rows, query), sortKey, sortDir),
    [rows, query, sortKey, sortDir],
  );

  const toggleSort = (key: SortKey): void => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const th: React.CSSProperties = {
    textAlign: "left",
    padding: "6px 12px",
    borderBottom: "2px solid #e5e7eb",
    cursor: "pointer",
    userSelect: "none",
  };
  const td: React.CSSProperties = { padding: "5px 12px", borderBottom: "1px solid #f1f5f9" };

  return (
    <div style={{ width: "100%", height: "100%", overflow: "auto", background: "#fff", fontFamily: "monospace", fontSize: 12 }}>
      <div style={{ padding: 12 }}>
        <input
          type="search"
          placeholder="filter clusters…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ padding: "4px 8px", fontFamily: "monospace", fontSize: 12, width: 220, border: "1px solid #e5e7eb", borderRadius: 4 }}
        />
        <span style={{ marginLeft: 12, color: "#6b7280" }}>{view.length} clusters</span>
      </div>
      {error && <div style={{ padding: 16, color: "#e55" }}>{error}</div>}
      <table style={{ borderCollapse: "collapse", width: "100%" }}>
        <thead>
          <tr>
            {COLUMNS.map((c) => (
              <th key={c.key} style={th} onClick={() => toggleSort(c.key)}>
                {c.label}
                {sortKey === c.key ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {view.map((r) => (
            <tr
              key={r.cluster_id}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectCluster(r.cluster_id)}
            >
              <td style={td}>cluster {r.cluster_id}</td>
              <td style={td}>{r.agent_count}</td>
              <td style={td}>{r.inter_cluster_edges}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
