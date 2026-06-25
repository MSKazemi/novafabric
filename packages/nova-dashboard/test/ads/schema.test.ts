import { describe, it, expect } from "vitest";
import { SCHEMA_IDS } from "../../src/ads/schema.js";

describe("SCHEMA_IDS", () => {
  it("has all four ADS v1 schema IDs", () => {
    expect(SCHEMA_IDS.metric_frame).toBe("ads.v1.metric_frame");
    expect(SCHEMA_IDS.cluster_layer).toBe("ads.v1.cluster_layer");
    expect(SCHEMA_IDS.subgraph_page_nodes).toBe("ads.v1.subgraph_page.nodes");
    expect(SCHEMA_IDS.subgraph_page_edges).toBe("ads.v1.subgraph_page.edges");
  });

  it("contains exactly four keys", () => {
    expect(Object.keys(SCHEMA_IDS)).toHaveLength(4);
  });
});
