import { describe, expect, it } from "vitest";

import {
  SCHEMA_IDS,
  ADS_SCHEMA_VERSION,
  CLUSTER_LOAD_BUDGET_MS,
  EXPAND_BUDGET_MS,
  LIVE_AGENT_BUDGET_MS,
  validateAdsSchemaId,
} from "../ads/schema.js";
import type { SchemaId, MetricFrameRow, ClusterLayerRow, SubgraphNodeRow, SubgraphEdgeRow } from "../ads/schema.js";

// ---------------------------------------------------------------------------
// Schema ID constants
// ---------------------------------------------------------------------------

describe("ADS v1 SCHEMA_IDS", () => {
  it("exports exactly 4 schema IDs", () => {
    expect(Object.keys(SCHEMA_IDS)).toHaveLength(4);
  });

  it("all schema IDs are strings", () => {
    for (const id of Object.values(SCHEMA_IDS)) {
      expect(typeof id).toBe("string");
    }
  });

  it("metric_frame ID matches ads.v1.metric_frame", () => {
    expect(SCHEMA_IDS.metric_frame).toBe("ads.v1.metric_frame");
  });

  it("cluster_layer ID matches ads.v1.cluster_layer", () => {
    expect(SCHEMA_IDS.cluster_layer).toBe("ads.v1.cluster_layer");
  });

  it("subgraph_page_nodes ID matches ads.v1.subgraph_page.nodes", () => {
    expect(SCHEMA_IDS.subgraph_page_nodes).toBe("ads.v1.subgraph_page.nodes");
  });

  it("subgraph_page_edges ID matches ads.v1.subgraph_page.edges", () => {
    expect(SCHEMA_IDS.subgraph_page_edges).toBe("ads.v1.subgraph_page.edges");
  });

  it("all schema IDs start with ads.v1.", () => {
    for (const id of Object.values(SCHEMA_IDS)) {
      expect(id.startsWith("ads.v1.")).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// Schema version constant
// ---------------------------------------------------------------------------

describe("ADS_SCHEMA_VERSION", () => {
  it("equals the exact string ads-v1", () => {
    expect(ADS_SCHEMA_VERSION).toBe("ads-v1");
  });

  it("is a string", () => {
    expect(typeof ADS_SCHEMA_VERSION).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// validateAdsSchemaId
// ---------------------------------------------------------------------------

describe("validateAdsSchemaId", () => {
  it("accepts all four standard schema IDs", () => {
    for (const id of Object.values(SCHEMA_IDS)) {
      expect(validateAdsSchemaId(id)).toBe(true);
    }
  });

  it("accepts a well-formed custom ads.v1.* ID", () => {
    expect(validateAdsSchemaId("ads.v1.custom_schema")).toBe(true);
  });

  it("accepts IDs with sub-segments (dots after ads.v1.)", () => {
    expect(validateAdsSchemaId("ads.v1.subgraph_page.nodes")).toBe(true);
  });

  it("rejects empty string", () => {
    expect(validateAdsSchemaId("")).toBe(false);
  });

  it("rejects IDs not starting with ads.v1.", () => {
    expect(validateAdsSchemaId("novafabric.topology.v1.nodes")).toBe(false);
    expect(validateAdsSchemaId("v1.metric_frame")).toBe(false);
    expect(validateAdsSchemaId("ads.metric_frame")).toBe(false);
  });

  it("rejects IDs with uppercase letters", () => {
    expect(validateAdsSchemaId("ads.v1.MetricFrame")).toBe(false);
    expect(validateAdsSchemaId("ADS.v1.metric_frame")).toBe(false);
  });

  it("rejects IDs with trailing dot", () => {
    expect(validateAdsSchemaId("ads.v1.")).toBe(false);
  });

  it("rejects IDs with spaces", () => {
    expect(validateAdsSchemaId("ads.v1.metric frame")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// FR timing budget constants
// ---------------------------------------------------------------------------

describe("FR timing budget constants", () => {
  it("CLUSTER_LOAD_BUDGET_MS is a number <= 500", () => {
    expect(typeof CLUSTER_LOAD_BUDGET_MS).toBe("number");
    expect(CLUSTER_LOAD_BUDGET_MS).toBeLessThanOrEqual(500);
  });

  it("EXPAND_BUDGET_MS is a number <= 500", () => {
    expect(typeof EXPAND_BUDGET_MS).toBe("number");
    expect(EXPAND_BUDGET_MS).toBeLessThanOrEqual(500);
  });

  it("LIVE_AGENT_BUDGET_MS is a number <= 5000", () => {
    expect(typeof LIVE_AGENT_BUDGET_MS).toBe("number");
    expect(LIVE_AGENT_BUDGET_MS).toBeLessThanOrEqual(5000);
  });

  it("all budgets are positive", () => {
    expect(CLUSTER_LOAD_BUDGET_MS).toBeGreaterThan(0);
    expect(EXPAND_BUDGET_MS).toBeGreaterThan(0);
    expect(LIVE_AGENT_BUDGET_MS).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// Row type shape assertions (compile-time + runtime structural checks)
// ---------------------------------------------------------------------------

describe("MetricFrameRow shape", () => {
  it("has all required numeric fields", () => {
    const row: MetricFrameRow = {
      agent_id: "agent-1",
      agent_type: "agent",
      status: "running",
      p50_latency_ms: 10,
      p95_latency_ms: 50,
      p99_latency_ms: 100,
      error_rate: 0.01,
      token_count_in: BigInt(200),
      token_count_out: BigInt(300),
      ts_start: BigInt(1_000_000),
      ts_end: BigInt(2_000_000),
    };
    expect(typeof row.p99_latency_ms).toBe("number");
    expect(typeof row.agent_id).toBe("string");
    expect(typeof row.token_count_in).toBe("bigint");
  });
});

describe("ClusterLayerRow shape", () => {
  it("has centroid_x and centroid_y as numbers", () => {
    const row: ClusterLayerRow = {
      cluster_id: BigInt(1),
      centroid_x: 3.14,
      centroid_y: -2.71,
      agent_count: BigInt(42),
      inter_cluster_edges: BigInt(7),
    };
    expect(typeof row.centroid_x).toBe("number");
    expect(typeof row.centroid_y).toBe("number");
    expect(typeof row.cluster_id).toBe("bigint");
  });
});

describe("SubgraphNodeRow shape", () => {
  it("has node_id, pos_x, pos_y", () => {
    const row: SubgraphNodeRow = {
      node_id: "agent-001",
      agent_type: "agent",
      status: "idle",
      cluster_id: BigInt(1),
      pos_x: 1.0,
      pos_y: 2.0,
    };
    expect(typeof row.node_id).toBe("string");
    expect(typeof row.pos_x).toBe("number");
    expect(typeof row.pos_y).toBe("number");
  });
});

describe("SubgraphEdgeRow shape", () => {
  it("has source_id, target_id, edge_type, cluster_id", () => {
    const row: SubgraphEdgeRow = {
      source_id: "a",
      target_id: "b",
      edge_type: "calls",
      cluster_id: BigInt(1),
    };
    expect(typeof row.source_id).toBe("string");
    expect(typeof row.target_id).toBe("string");
    expect(typeof row.edge_type).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// SchemaId union type exhaustiveness
// ---------------------------------------------------------------------------

describe("SchemaId type values", () => {
  it("SchemaId includes all four SCHEMA_IDS values", () => {
    const ids: SchemaId[] = [
      "ads.v1.metric_frame",
      "ads.v1.cluster_layer",
      "ads.v1.subgraph_page.nodes",
      "ads.v1.subgraph_page.edges",
    ];
    for (const id of ids) {
      expect(typeof id).toBe("string");
    }
    expect(ids).toHaveLength(4);
  });
});
