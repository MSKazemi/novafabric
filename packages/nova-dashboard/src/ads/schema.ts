/**
 * ADS v1 — Arrow Dashboard Schema constants and types.
 *
 * Mirrors the Python ads_encoder.py schemas; kept in sync manually.
 * The schema_id values are the ground truth for receiver dispatch.
 */

export const SCHEMA_IDS = {
  metric_frame: "ads.v1.metric_frame",
  cluster_layer: "ads.v1.cluster_layer",
  subgraph_page_nodes: "ads.v1.subgraph_page.nodes",
  subgraph_page_edges: "ads.v1.subgraph_page.edges",
} as const;

export type SchemaId = (typeof SCHEMA_IDS)[keyof typeof SCHEMA_IDS];

/** ADS schema version string embedded in every IPC batch's custom metadata. */
export const ADS_SCHEMA_VERSION = "ads-v1";

// -----------------------------------------------------------------------
// FR timing budgets (acceptance criteria: tc-005, tc-006, tc-007)
// -----------------------------------------------------------------------

/** FR-10: cluster-layer load must complete within this many milliseconds. */
export const CLUSTER_LOAD_BUDGET_MS = 500;

/** FR-11: click-to-expand subgraph must render within this many milliseconds. */
export const EXPAND_BUDGET_MS = 500;

/** FR-13: new live agent must appear on screen within this many milliseconds. */
export const LIVE_AGENT_BUDGET_MS = 5000;

/**
 * Validates that a schema_id string conforms to the ADS v1 naming convention:
 * dot-separated segments, starting with "ads.v1.".
 */
export function validateAdsSchemaId(schemaId: string): boolean {
  return /^ads\.v1\.[a-z0-9_.]+$/.test(schemaId);
}

// Row types matching the Arrow schemas on the server side.

export interface MetricFrameRow {
  agent_id: string;
  agent_type: string;
  status: string;
  p50_latency_ms: number;
  p95_latency_ms: number;
  p99_latency_ms: number;
  error_rate: number;
  token_count_in: bigint;
  token_count_out: bigint;
  ts_start: bigint; // ms since epoch
  ts_end: bigint;
}

export interface ClusterLayerRow {
  cluster_id: bigint;
  centroid_x: number;
  centroid_y: number;
  agent_count: bigint;
  inter_cluster_edges: bigint;
}

export interface SubgraphNodeRow {
  node_id: string;
  agent_type: string;
  status: string;
  cluster_id: bigint;
  pos_x: number;
  pos_y: number;
}

export interface SubgraphEdgeRow {
  source_id: string;
  target_id: string;
  edge_type: string;
  cluster_id: bigint;
}

/**
 * Reads the schema_id from an Arrow IPC batch's custom metadata.
 * Returns null if the bytes are not valid Arrow IPC or metadata is absent.
 */
export function readSchemaId(ipc: Uint8Array): SchemaId | null {
  // The schema_id is encoded in the flatbuffer metadata of the schema message.
  // apache-arrow exposes it via RecordBatchReader.
  try {
    const { tableFromIPC } = require("apache-arrow") as typeof import("apache-arrow");
    const table = tableFromIPC(ipc);
    const meta = table.schema.metadata;
    if (!meta) return null;
    const id = meta.get("schema_id");
    if (!id) return null;
    return id as SchemaId;
  } catch {
    return null;
  }
}
