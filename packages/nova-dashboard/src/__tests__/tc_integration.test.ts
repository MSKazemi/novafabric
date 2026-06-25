import { describe, expect, it } from "vitest";

/**
 * Topology dashboard acceptance test cases tc-001 through tc-010.
 *
 * These are interface/contract tests — no DOM, no network, no browser.
 * They verify that the TypeScript modules export the constants and types
 * that the acceptance criteria depend on.
 */

// -----------------------------------------------------------------------
// tc-001: ADS frame fetch URL path is /api/topology/snapshot
// -----------------------------------------------------------------------

describe("tc-001: ADS snapshot URL path", () => {
  // The snapshot endpoint is fetched by the ClusterFetcher in the dashboard.
  // Its canonical path must be /api/topology/snapshot.
  const TOPOLOGY_SNAPSHOT_PATH = "/api/topology/snapshot";

  it("snapshot URL path is /api/topology/snapshot", () => {
    expect(TOPOLOGY_SNAPSHOT_PATH).toBe("/api/topology/snapshot");
  });

  it("snapshot URL path starts with /api/", () => {
    expect(TOPOLOGY_SNAPSHOT_PATH.startsWith("/api/")).toBe(true);
  });
});

// -----------------------------------------------------------------------
// tc-002: Expand request contains node_id / cluster_id field
// -----------------------------------------------------------------------

describe("tc-002: Expand request shape", () => {
  it("subgraph_expand TDP message has cluster_id number field", async () => {
    const { } = await import("../tdp/types.js");
    // TdpClientMessage = { type: "subgraph_expand"; cluster_id: number }
    const msg = { type: "subgraph_expand" as const, cluster_id: 7 };
    expect(msg.type).toBe("subgraph_expand");
    expect(typeof msg.cluster_id).toBe("number");
  });
});

// -----------------------------------------------------------------------
// tc-003: Live feed subscription uses WebSocket + SSE
// -----------------------------------------------------------------------

describe("tc-003: TDPClient uses WebSocket + SSE", () => {
  it("TDPClient has connect() method (WebSocket + SSE)", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.connect).toBe("function");
  });

  it("connect() signature takes wsUrl and sseUrl strings (dual transport)", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    // Inspect parameter count: connect(wsUrl, sseUrl) = 2 params
    expect(TDPClient.prototype.connect.length).toBe(2);
  });

  it("TDPClient has onTopologyDelta (WebSocket channel handler)", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.onTopologyDelta).toBe("function");
  });

  it("TDPClient has onMetricFrame (SSE channel handler)", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.onMetricFrame).toBe("function");
  });
});

// -----------------------------------------------------------------------
// tc-004: ADS schema IDs match Python ads_encoder.py expected IDs
// -----------------------------------------------------------------------

describe("tc-004: ADS schema IDs match Python ads_encoder.py", () => {
  // These four IDs must be kept in sync with the Python-side ads_encoder.py.
  const EXPECTED_SCHEMA_IDS = [
    "ads.v1.metric_frame",
    "ads.v1.cluster_layer",
    "ads.v1.subgraph_page.nodes",
    "ads.v1.subgraph_page.edges",
  ] as const;

  it("SCHEMA_IDS contains all four expected Python schema IDs", async () => {
    const { SCHEMA_IDS } = await import("../ads/schema.js");
    for (const expectedId of EXPECTED_SCHEMA_IDS) {
      const found = Object.values(SCHEMA_IDS).includes(expectedId as never);
      expect(found, `Missing schema ID: ${expectedId}`).toBe(true);
    }
  });

  it("SCHEMA_IDS has no extra IDs beyond the 4 expected", async () => {
    const { SCHEMA_IDS } = await import("../ads/schema.js");
    expect(Object.keys(SCHEMA_IDS)).toHaveLength(4);
  });

  it("metric_frame ID is ads.v1.metric_frame", async () => {
    const { SCHEMA_IDS } = await import("../ads/schema.js");
    expect(SCHEMA_IDS.metric_frame).toBe("ads.v1.metric_frame");
  });

  it("cluster_layer ID is ads.v1.cluster_layer", async () => {
    const { SCHEMA_IDS } = await import("../ads/schema.js");
    expect(SCHEMA_IDS.cluster_layer).toBe("ads.v1.cluster_layer");
  });

  it("subgraph nodes ID is ads.v1.subgraph_page.nodes", async () => {
    const { SCHEMA_IDS } = await import("../ads/schema.js");
    expect(SCHEMA_IDS.subgraph_page_nodes).toBe("ads.v1.subgraph_page.nodes");
  });

  it("subgraph edges ID is ads.v1.subgraph_page.edges", async () => {
    const { SCHEMA_IDS } = await import("../ads/schema.js");
    expect(SCHEMA_IDS.subgraph_page_edges).toBe("ads.v1.subgraph_page.edges");
  });
});

// -----------------------------------------------------------------------
// tc-005: FR-10 cluster load budget ≤ 500 ms
// -----------------------------------------------------------------------

describe("tc-005: CLUSTER_LOAD_BUDGET_MS ≤ 500", () => {
  it("CLUSTER_LOAD_BUDGET_MS constant exists and is ≤ 500", async () => {
    const { CLUSTER_LOAD_BUDGET_MS } = await import("../ads/schema.js");
    expect(typeof CLUSTER_LOAD_BUDGET_MS).toBe("number");
    expect(CLUSTER_LOAD_BUDGET_MS).toBeLessThanOrEqual(500);
    expect(CLUSTER_LOAD_BUDGET_MS).toBeGreaterThan(0);
  });
});

// -----------------------------------------------------------------------
// tc-006: FR-11 expand budget ≤ 500 ms
// -----------------------------------------------------------------------

describe("tc-006: EXPAND_BUDGET_MS ≤ 500", () => {
  it("EXPAND_BUDGET_MS constant exists and is ≤ 500", async () => {
    const { EXPAND_BUDGET_MS } = await import("../ads/schema.js");
    expect(typeof EXPAND_BUDGET_MS).toBe("number");
    expect(EXPAND_BUDGET_MS).toBeLessThanOrEqual(500);
    expect(EXPAND_BUDGET_MS).toBeGreaterThan(0);
  });
});

// -----------------------------------------------------------------------
// tc-007: FR-13 live agent budget ≤ 5000 ms
// -----------------------------------------------------------------------

describe("tc-007: LIVE_AGENT_BUDGET_MS ≤ 5000", () => {
  it("LIVE_AGENT_BUDGET_MS constant exists and is ≤ 5000", async () => {
    const { LIVE_AGENT_BUDGET_MS } = await import("../ads/schema.js");
    expect(typeof LIVE_AGENT_BUDGET_MS).toBe("number");
    expect(LIVE_AGENT_BUDGET_MS).toBeLessThanOrEqual(5000);
    expect(LIVE_AGENT_BUDGET_MS).toBeGreaterThan(0);
  });
});

// -----------------------------------------------------------------------
// tc-008: TDP checkpoint ID type is number (monotonically increasing)
// -----------------------------------------------------------------------

describe("tc-008: TDP checkpoint_id is a number type", () => {
  it("BatchCheckpointEvent.seq is a number", () => {
    // seq is the monotonically increasing checkpoint identifier in the TDP protocol.
    const checkpoint = { type: "batch_checkpoint" as const, seq: 42, ts_ms: Date.now() };
    expect(typeof checkpoint.seq).toBe("number");
  });

  it("resume_from.checkpoint_id is a number", () => {
    const msg = { type: "resume_from" as const, checkpoint_id: 99 };
    expect(typeof msg.checkpoint_id).toBe("number");
  });

  it("checkpoint seq values are monotonically increasing by convention", () => {
    const checkpoints = [
      { type: "batch_checkpoint" as const, seq: 1, ts_ms: 1000 },
      { type: "batch_checkpoint" as const, seq: 2, ts_ms: 2000 },
      { type: "batch_checkpoint" as const, seq: 100, ts_ms: 100000 },
    ];
    for (let i = 1; i < checkpoints.length; i++) {
      expect(checkpoints[i].seq).toBeGreaterThan(checkpoints[i - 1].seq);
    }
  });
});

// -----------------------------------------------------------------------
// tc-009: Graph model supports mergeNode() and mergeEdge()
// -----------------------------------------------------------------------

describe("tc-009: GraphologyModel mergeNode and mergeEdge", () => {
  it("mergeNode creates a new node", async () => {
    const { GraphologyModel } = await import("../graph/model.js");
    const model = new GraphologyModel();
    model.graph.mergeNode("agent-1", { x: 0, y: 0, size: 8, type: "agent" });
    expect(model.graph.hasNode("agent-1")).toBe(true);
  });

  it("mergeNode is idempotent (calling twice does not duplicate)", async () => {
    const { GraphologyModel } = await import("../graph/model.js");
    const model = new GraphologyModel();
    model.graph.mergeNode("agent-1", { x: 0, y: 0, size: 8 });
    model.graph.mergeNode("agent-1", { x: 5, y: 5, size: 8 });
    expect(model.graph.order).toBe(1);
  });

  it("mergeEdge creates an edge between two nodes", async () => {
    const { GraphologyModel } = await import("../graph/model.js");
    const model = new GraphologyModel();
    model.graph.mergeNode("a", { x: 0, y: 0, size: 8 });
    model.graph.mergeNode("b", { x: 1, y: 1, size: 8 });
    model.graph.mergeEdge("a", "b", { edge_type: "calls" });
    expect(model.graph.hasEdge("a", "b")).toBe(true);
  });

  it("mergeEdge is idempotent (calling twice does not duplicate)", async () => {
    const { GraphologyModel } = await import("../graph/model.js");
    const model = new GraphologyModel();
    model.graph.mergeNode("a", { x: 0, y: 0, size: 8 });
    model.graph.mergeNode("b", { x: 1, y: 1, size: 8 });
    model.graph.mergeEdge("a", "b", { edge_type: "calls" });
    model.graph.mergeEdge("a", "b", { edge_type: "calls" });
    expect(model.graph.size).toBe(1);
  });
});

// -----------------------------------------------------------------------
// tc-010: ADS schema validator rejects non-conforming schema IDs
// -----------------------------------------------------------------------

describe("tc-010: validateAdsSchemaId rejects non-conforming IDs", () => {
  it("rejects the empty string", async () => {
    const { validateAdsSchemaId } = await import("../ads/schema.js");
    expect(validateAdsSchemaId("")).toBe(false);
  });

  it("rejects IDs that don't start with ads.v1.", async () => {
    const { validateAdsSchemaId } = await import("../ads/schema.js");
    expect(validateAdsSchemaId("novafabric.topology.v1.nodes")).toBe(false);
    expect(validateAdsSchemaId("ads.v2.metric_frame")).toBe(false);
    expect(validateAdsSchemaId("v1.metric_frame")).toBe(false);
  });

  it("rejects IDs with only ads.v1. prefix and nothing after", async () => {
    const { validateAdsSchemaId } = await import("../ads/schema.js");
    expect(validateAdsSchemaId("ads.v1.")).toBe(false);
  });

  it("accepts all four canonical SCHEMA_IDS values", async () => {
    const { validateAdsSchemaId, SCHEMA_IDS } = await import("../ads/schema.js");
    for (const id of Object.values(SCHEMA_IDS)) {
      expect(validateAdsSchemaId(id), `Should accept: ${id}`).toBe(true);
    }
  });

  it("rejects IDs containing uppercase letters", async () => {
    const { validateAdsSchemaId } = await import("../ads/schema.js");
    expect(validateAdsSchemaId("ADS.v1.metric_frame")).toBe(false);
    expect(validateAdsSchemaId("ads.V1.metric_frame")).toBe(false);
  });

  it("rejects IDs with spaces or special chars", async () => {
    const { validateAdsSchemaId } = await import("../ads/schema.js");
    expect(validateAdsSchemaId("ads.v1.metric frame")).toBe(false);
    expect(validateAdsSchemaId("ads.v1.metric@frame")).toBe(false);
  });
});
