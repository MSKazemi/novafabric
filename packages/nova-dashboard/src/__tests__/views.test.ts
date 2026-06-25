import { describe, expect, it } from "vitest";
import { computeTreemap, type ClusterRow } from "../views/treemap.js";
import { sortClusters, filterClusters } from "../views/clusterTable.js";
import { computeLayeredLayout } from "../graph/layoutDagre.js";

const CLUSTERS: ClusterRow[] = [
  { cluster_id: 60, agent_count: 106, inter_cluster_edges: 4 },
  { cluster_id: 16, agent_count: 17, inter_cluster_edges: 2 },
  { cluster_id: 66, agent_count: 10, inter_cluster_edges: 1 },
  { cluster_id: 3, agent_count: 1, inter_cluster_edges: 0 },
];

describe("computeTreemap", () => {
  it("covers the full box and gives larger clusters larger area", () => {
    const cells = computeTreemap(CLUSTERS, 1000, 500);
    expect(cells).toHaveLength(4);
    const area = (id: number) => {
      const c = cells.find((x) => x.cluster_id === id)!;
      return (c.x1 - c.x0) * (c.y1 - c.y0);
    };
    // cluster 60 (106 agents) must occupy more area than cluster 16 (17 agents)
    expect(area(60)).toBeGreaterThan(area(16));
    expect(area(16)).toBeGreaterThan(area(66));
    expect(area(66)).toBeGreaterThan(area(3));
  });

  it("returns empty for zero-size box or empty input", () => {
    expect(computeTreemap(CLUSTERS, 0, 100)).toEqual([]);
    expect(computeTreemap([], 100, 100)).toEqual([]);
  });

  it("skips non-positive agent counts", () => {
    const cells = computeTreemap(
      [{ cluster_id: 1, agent_count: 0, inter_cluster_edges: 0 }],
      100,
      100,
    );
    expect(cells).toEqual([]);
  });
});

describe("sortClusters / filterClusters", () => {
  it("sorts by agent_count descending", () => {
    const ids = sortClusters(CLUSTERS, "agent_count", "desc").map((c) => c.cluster_id);
    expect(ids).toEqual([60, 16, 66, 3]);
  });

  it("sorts by cluster_id ascending", () => {
    const ids = sortClusters(CLUSTERS, "cluster_id", "asc").map((c) => c.cluster_id);
    expect(ids).toEqual([3, 16, 60, 66]);
  });

  it("does not mutate the input array", () => {
    const before = [...CLUSTERS];
    sortClusters(CLUSTERS, "agent_count", "asc");
    expect(CLUSTERS).toEqual(before);
  });

  it("filters by cluster id text and exact id", () => {
    expect(filterClusters(CLUSTERS, "60").map((c) => c.cluster_id)).toEqual([60]);
    expect(filterClusters(CLUSTERS, "cluster 16").map((c) => c.cluster_id)).toEqual([16]);
    expect(filterClusters(CLUSTERS, "")).toHaveLength(4);
  });
});

describe("computeLayeredLayout", () => {
  it("places a source above its target (TB rankdir)", () => {
    const pos = computeLayeredLayout(
      [{ id: "run:a" }, { id: "model:x" }],
      [{ source: "run:a", target: "model:x" }],
    );
    const run = pos.get("run:a")!;
    const model = pos.get("model:x")!;
    // y is flipped so sources sit visually higher (larger y)
    expect(run.y).toBeGreaterThan(model.y);
  });

  it("returns a position for every node, including isolated ones", () => {
    const pos = computeLayeredLayout(
      [{ id: "a" }, { id: "b" }, { id: "lonely" }],
      [{ source: "a", target: "b" }],
    );
    expect(pos.has("a")).toBe(true);
    expect(pos.has("b")).toBe(true);
    expect(pos.has("lonely")).toBe(true);
  });

  it("returns empty map for no nodes", () => {
    expect(computeLayeredLayout([], []).size).toBe(0);
  });
});
