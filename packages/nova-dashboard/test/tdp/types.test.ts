/**
 * Type-level tests for TDP discriminated union — these are compile-time
 * checks expressed as runtime assertions on the type constants.
 */
import { describe, it, expect } from "vitest";
import type { TdpDeltaEvent, BatchCheckpointEvent } from "../../src/tdp/types.js";

describe("TDP type constants", () => {
  it("BatchCheckpointEvent matches shape", () => {
    const ev: BatchCheckpointEvent = { type: "batch_checkpoint", seq: 1, ts_ms: Date.now() };
    expect(ev.type).toBe("batch_checkpoint");
    expect(typeof ev.seq).toBe("number");
    expect(typeof ev.ts_ms).toBe("number");
  });

  it("TdpDeltaEvent discriminant add_node works", () => {
    const ev: TdpDeltaEvent = {
      type: "add_node",
      seq: 2,
      node_id: "agent-1",
      agent_type: "llm",
      status: "active",
      cluster_id: 0,
      pos_x: 1.5,
      pos_y: 2.5,
    };
    expect(ev.type).toBe("add_node");
  });

  it("TdpDeltaEvent discriminant topology_reset works", () => {
    const ev: TdpDeltaEvent = { type: "topology_reset", seq: 3 };
    expect(ev.type).toBe("topology_reset");
  });
});
