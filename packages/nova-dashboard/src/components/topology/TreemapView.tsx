/**
 * TreemapView — cluster sizes at a glance.
 *
 * Each rectangle's area is proportional to the cluster's agent count, so the
 * big clusters are immediately obvious. Clicking a cell asks the parent to
 * expand that cluster (and typically switch to the cluster graph view).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { computeTreemap, type ClusterRow } from "../../views/treemap.js";

interface Props {
  token: string;
  onSelectCluster: (clusterId: number) => void;
}

function buildUrl(token: string): string {
  return `${window.location.origin}/topology/cluster-list?token=${token}`;
}

function cellColor(agentCount: number): string {
  if (agentCount <= 1) return "#cbd5e1";
  if (agentCount < 10) return "#93c5fd";
  if (agentCount < 50) return "#3b82f6";
  return "#1d4ed8";
}

export function TreemapView({ token, onSelectCluster }: Props): React.ReactElement {
  const ref = useRef<HTMLDivElement>(null);
  const [rows, setRows] = useState<ClusterRow[]>([]);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetch(buildUrl(token))
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => setRows(data as ClusterRow[]))
      .catch((e: unknown) => setError(String(e)));
  }, [token]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const cells = useMemo(
    () => computeTreemap(rows, size.w, size.h),
    [rows, size.w, size.h],
  );

  return (
    <div ref={ref} style={{ width: "100%", height: "100%", position: "relative", background: "#f8fafc" }}>
      {error && (
        <div style={{ padding: 16, color: "#e55", fontFamily: "monospace" }}>{error}</div>
      )}
      {!error && rows.length === 0 && (
        <div style={{ padding: 16, color: "#6b7280", fontFamily: "monospace" }}>
          No clusters yet.
        </div>
      )}
      <svg width={size.w} height={size.h} style={{ display: "block" }}>
        {cells.map((c) => {
          const w = c.x1 - c.x0;
          const h = c.y1 - c.y0;
          const showLabel = w > 56 && h > 24;
          return (
            <g
              key={c.cluster_id}
              transform={`translate(${c.x0},${c.y0})`}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectCluster(c.cluster_id)}
            >
              <rect
                width={w}
                height={h}
                fill={cellColor(c.agent_count)}
                stroke="#ffffff"
                strokeWidth={1}
                rx={3}
              />
              {showLabel && (
                <text
                  x={6}
                  y={16}
                  fontSize={11}
                  fontFamily="monospace"
                  fill={c.agent_count >= 10 ? "#ffffff" : "#1e293b"}
                >
                  {`cluster ${c.cluster_id} (${c.agent_count})`}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
