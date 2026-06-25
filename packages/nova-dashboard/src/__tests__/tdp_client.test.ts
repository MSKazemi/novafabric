import { describe, expect, it } from "vitest";

import type {
  TdpDeltaEvent,
  TdpClientMessage,
  AddNodeEvent,
  RemoveNodeEvent,
  AddEdgeEvent,
  RemoveEdgeEvent,
  UpdatePropertyEvent,
  BatchCheckpointEvent,
  TopologyResetEvent,
  MetricSummaryEvent,
} from "../tdp/types.js";

import type {
  DeltaHandler,
  MetricHandler,
  ExpandHandler,
} from "../tdp/client.js";

// -----------------------------------------------------------------------
// TDPClient class shape
// -----------------------------------------------------------------------

describe("TDPClient class shape", () => {
  it("exports TDPClient as a class (function)", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient).toBe("function");
  });

  it("TDPClient prototype has connect method", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.connect).toBe("function");
  });

  it("TDPClient prototype has disconnect method", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.disconnect).toBe("function");
  });

  it("TDPClient prototype has onTopologyDelta method", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.onTopologyDelta).toBe("function");
  });

  it("TDPClient prototype has onMetricFrame method", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.onMetricFrame).toBe("function");
  });

  it("TDPClient prototype has onSubgraphExpand method", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.onSubgraphExpand).toBe("function");
  });

  it("TDPClient prototype has sendExpandRequest method", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.sendExpandRequest).toBe("function");
  });

  it("TDPClient prototype has sendCollapseRequest method", async () => {
    const { TDPClient } = await import("../tdp/client.js");
    expect(typeof TDPClient.prototype.sendCollapseRequest).toBe("function");
  });
});

// -----------------------------------------------------------------------
// TdpDeltaEvent discriminated union — type discriminants
// -----------------------------------------------------------------------

describe("TdpDeltaEvent type discriminants", () => {
  const ADD_NODE_TYPE = "add_node" as const;
  const REMOVE_NODE_TYPE = "remove_node" as const;
  const ADD_EDGE_TYPE = "add_edge" as const;
  const REMOVE_EDGE_TYPE = "remove_edge" as const;
  const UPDATE_PROP_TYPE = "update_property" as const;
  const CHECKPOINT_TYPE = "batch_checkpoint" as const;
  const RESET_TYPE = "topology_reset" as const;

  it("add_node event has correct shape", () => {
    const ev: AddNodeEvent = {
      type: ADD_NODE_TYPE,
      seq: 1,
      node_id: "agent-001",
      agent_type: "agent",
      status: "running",
      cluster_id: 0,
      pos_x: 1.0,
      pos_y: 2.0,
    };
    expect(ev.type).toBe("add_node");
    expect(typeof ev.node_id).toBe("string");
    expect(typeof ev.cluster_id).toBe("number");
    expect(typeof ev.pos_x).toBe("number");
  });

  it("remove_node event has node_id field", () => {
    const ev: RemoveNodeEvent = { type: REMOVE_NODE_TYPE, seq: 2, node_id: "agent-001" };
    expect(ev.type).toBe("remove_node");
    expect(typeof ev.node_id).toBe("string");
  });

  it("add_edge event has source_id, target_id, edge_type, cluster_id", () => {
    const ev: AddEdgeEvent = {
      type: ADD_EDGE_TYPE,
      seq: 3,
      source_id: "a",
      target_id: "b",
      edge_type: "calls",
      cluster_id: 0,
    };
    expect(ev.type).toBe("add_edge");
    expect(typeof ev.source_id).toBe("string");
    expect(typeof ev.target_id).toBe("string");
    expect(typeof ev.edge_type).toBe("string");
  });

  it("remove_edge event has source_id and target_id", () => {
    const ev: RemoveEdgeEvent = {
      type: REMOVE_EDGE_TYPE,
      seq: 4,
      source_id: "a",
      target_id: "b",
    };
    expect(ev.type).toBe("remove_edge");
  });

  it("update_property event has node_id, key, value", () => {
    const ev: UpdatePropertyEvent = {
      type: UPDATE_PROP_TYPE,
      seq: 5,
      node_id: "agent-001",
      key: "status",
      value: "idle",
    };
    expect(ev.type).toBe("update_property");
    expect(typeof ev.key).toBe("string");
    expect(typeof ev.value).toBe("string");
  });

  it("batch_checkpoint has seq field (monotonically increasing checkpoint ID)", () => {
    const ev: BatchCheckpointEvent = { type: CHECKPOINT_TYPE, seq: 42, ts_ms: Date.now() };
    expect(ev.type).toBe("batch_checkpoint");
    expect(typeof ev.seq).toBe("number");
    expect(typeof ev.ts_ms).toBe("number");
  });

  it("topology_reset has seq field", () => {
    const ev: TopologyResetEvent = { type: RESET_TYPE, seq: 100 };
    expect(ev.type).toBe("topology_reset");
    expect(typeof ev.seq).toBe("number");
  });
});

// -----------------------------------------------------------------------
// TdpClientMessage (client → server) discriminants
// -----------------------------------------------------------------------

describe("TdpClientMessage shapes", () => {
  it("subgraph_expand message has cluster_id: number", () => {
    const msg: TdpClientMessage = { type: "subgraph_expand", cluster_id: 3 };
    expect(msg.type).toBe("subgraph_expand");
    if (msg.type === "subgraph_expand") {
      expect(typeof msg.cluster_id).toBe("number");
    }
  });

  it("subgraph_collapse message has cluster_id: number", () => {
    const msg: TdpClientMessage = { type: "subgraph_collapse", cluster_id: 3 };
    expect(msg.type).toBe("subgraph_collapse");
  });

  it("resume_from message has checkpoint_id: number", () => {
    const msg: TdpClientMessage = { type: "resume_from", checkpoint_id: 99 };
    expect(msg.type).toBe("resume_from");
    if (msg.type === "resume_from") {
      expect(typeof msg.checkpoint_id).toBe("number");
    }
  });
});

// -----------------------------------------------------------------------
// MetricSummaryEvent shape
// -----------------------------------------------------------------------

describe("MetricSummaryEvent shape", () => {
  it("has ts_ms, node_count, edge_count, cluster_count as numbers", () => {
    const ev: MetricSummaryEvent = {
      ts_ms: 1_716_000_000,
      node_count: 100,
      edge_count: 200,
      cluster_count: 5,
    };
    expect(typeof ev.ts_ms).toBe("number");
    expect(typeof ev.node_count).toBe("number");
    expect(typeof ev.edge_count).toBe("number");
    expect(typeof ev.cluster_count).toBe("number");
  });
});

// -----------------------------------------------------------------------
// Handler type signatures
// -----------------------------------------------------------------------

describe("TDPClient handler type signatures", () => {
  it("DeltaHandler accepts a TdpDeltaEvent", () => {
    const calls: TdpDeltaEvent[] = [];
    const handler: DeltaHandler = (ev) => calls.push(ev);
    const ev: TopologyResetEvent = { type: "topology_reset", seq: 1 };
    handler(ev);
    expect(calls).toHaveLength(1);
    expect(calls[0].type).toBe("topology_reset");
  });

  it("MetricHandler accepts a MetricSummaryEvent", () => {
    const calls: MetricSummaryEvent[] = [];
    const handler: MetricHandler = (ev) => calls.push(ev);
    handler({ ts_ms: 1, node_count: 2, edge_count: 3, cluster_count: 4 });
    expect(calls[0].node_count).toBe(2);
  });

  it("ExpandHandler accepts (clusterId, nodesBatch, edgesBatch)", () => {
    const calls: Array<[number, Uint8Array, Uint8Array]> = [];
    const handler: ExpandHandler = (cid, nb, eb) => calls.push([cid, nb, eb]);
    const nodesBatch = new Uint8Array([1, 2, 3]);
    const edgesBatch = new Uint8Array([4, 5, 6]);
    handler(7, nodesBatch, edgesBatch);
    expect(calls[0][0]).toBe(7);
    expect(calls[0][1]).toBe(nodesBatch);
    expect(calls[0][2]).toBe(edgesBatch);
  });
});
