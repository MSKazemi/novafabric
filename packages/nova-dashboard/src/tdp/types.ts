/**
 * TDP v1 — Topology Delta Protocol discriminated union types.
 *
 * Server-to-client events carried over the nova-tdp-v1 WebSocket subprotocol
 * and the /metrics/stream SSE channel.
 */

export type TdpDeltaEvent =
  | AddNodeEvent
  | RemoveNodeEvent
  | AddEdgeEvent
  | RemoveEdgeEvent
  | UpdatePropertyEvent
  | BatchCheckpointEvent
  | TopologyResetEvent;

export interface AddNodeEvent {
  type: "add_node";
  seq: number;
  node_id: string;
  agent_type: string;
  status: string;
  cluster_id: number;
  pos_x: number;
  pos_y: number;
}

export interface RemoveNodeEvent {
  type: "remove_node";
  seq: number;
  node_id: string;
}

export interface AddEdgeEvent {
  type: "add_edge";
  seq: number;
  source_id: string;
  target_id: string;
  edge_type: string;
  cluster_id: number;
}

export interface RemoveEdgeEvent {
  type: "remove_edge";
  seq: number;
  source_id: string;
  target_id: string;
}

export interface UpdatePropertyEvent {
  type: "update_property";
  seq: number;
  node_id: string;
  key: string;
  value: string;
}

export interface BatchCheckpointEvent {
  type: "batch_checkpoint";
  seq: number;
  ts_ms: number;
}

export interface TopologyResetEvent {
  type: "topology_reset";
  seq: number;
}

// Client-to-server messages
export type TdpClientMessage =
  | { type: "subgraph_expand"; cluster_id: number }
  | { type: "subgraph_collapse"; cluster_id: number }
  | { type: "resume_from"; checkpoint_id: number };

// SSE metric summary event
export interface MetricSummaryEvent {
  ts_ms: number;
  node_count: number;
  edge_count: number;
  cluster_count: number;
}
