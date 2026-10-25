/**
 * TDPClient — WebSocket + SSE connection manager for the topology dashboard.
 *
 * Connects to the TDP v1 WebSocket at /topology/stream and the SSE metrics
 * stream at /metrics/stream. Handles reconnection with exponential backoff
 * and gap recovery via resume_from on reconnect.
 *
 * A-1: Live delta push — the server now calls DeltaBuffer.subscribe() per
 * connection and sends delta events immediately via send_bytes() instead of
 * waiting for the 10-second heartbeat cycle.
 *
 * A-2: All TDP delta events (add_node, remove_node, add_edge, remove_edge,
 * update_property, batch_checkpoint, topology_reset) are now sent as binary
 * Arrow IPC frames (ADS v1 delta_event schema).  The subgraph_expand nodes
 * and edges frames remain binary Arrow IPC with their own schemas.
 * This client decodes both frame types from the same onmessage handler.
 */

import { type Table, tableFromIPC } from "apache-arrow";

import type {
  TdpDeltaEvent,
  TdpClientMessage,
  MetricSummaryEvent,
  BatchCheckpointEvent,
} from "./types.js";

const MAX_BACKOFF_MS = 10_000;
const INITIAL_BACKOFF_MS = 250;

export type DeltaHandler = (event: TdpDeltaEvent) => void;
export type MetricHandler = (event: MetricSummaryEvent) => void;
export type ClusterFetcher = (token: string) => Promise<Uint8Array>;
export type ExpandHandler = (clusterId: number, nodesBatch: Uint8Array, edgesBatch: Uint8Array) => void;

export class TDPClient {
  private ws: WebSocket | null = null;
  private sse: EventSource | null = null;
  private lastCheckpointId = 0;
  private backoffMs = INITIAL_BACKOFF_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;

  private deltaHandlers: DeltaHandler[] = [];
  private metricHandlers: MetricHandler[] = [];
  private expandHandlers: ExpandHandler[] = [];
  /**
   * Expand requests awaiting their two binary frames (nodes, then edges), oldest first.
   * The server answers requests in order, but a click on B before A's frames arrive must not
   * re-assign A's frames to B (review go-collector-ts-packages-14).
   */
  private _pendingExpands: Array<{ clusterId: number; nodes: Uint8Array | null }> = [];

  /**
   * Open WS + SSE connections.
   * @param wsUrl   e.g. "ws://127.0.0.1:4321/topology/stream?token=..."
   * @param sseUrl  e.g. "http://127.0.0.1:4321/metrics/stream?token=..."
   */
  connect(wsUrl: string, sseUrl: string): void {
    this.closed = false;
    this._connectWs(wsUrl);
    this._connectSse(sseUrl);
  }

  disconnect(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.sse?.close();
    this.ws = null;
    this.sse = null;
  }

  onTopologyDelta(handler: DeltaHandler): void {
    this.deltaHandlers.push(handler);
  }

  onMetricFrame(handler: MetricHandler): void {
    this.metricHandlers.push(handler);
  }

  onSubgraphExpand(handler: ExpandHandler): void {
    this.expandHandlers.push(handler);
  }

  sendExpandRequest(clusterId: number): void {
    // Pending state exists only for a request that was actually sent: a dropped request
    // would otherwise make the next unrelated binary frame read as a nodes batch.
    if (this._pendingExpands.some((p) => p.clusterId === clusterId)) return;
    if (this._sendMessage({ type: "subgraph_expand", cluster_id: clusterId })) {
      this._pendingExpands.push({ clusterId, nodes: null });
    }
  }

  sendCollapseRequest(clusterId: number): void {
    this._sendMessage({ type: "subgraph_collapse", cluster_id: clusterId });
  }

  // -----------------------------------------------------------------------
  // Private
  // -----------------------------------------------------------------------

  /** True when the message was handed to an open socket. */
  private _sendMessage(msg: TdpClientMessage): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  private _connectWs(wsUrl: string): void {
    const ws = new WebSocket(wsUrl, ["nova-tdp-v1"]);
    this.ws = ws;

    ws.onopen = () => {
      this.backoffMs = INITIAL_BACKOFF_MS;
      if (this.lastCheckpointId > 0) {
        // Gap recovery on reconnect
        this._sendMessage({ type: "resume_from", checkpoint_id: this.lastCheckpointId });
      }
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        this._handleTextFrame(ev.data);
      } else if (ev.data instanceof ArrayBuffer || ev.data instanceof Blob) {
        // Binary Arrow IPC frame — currently handled by GraphologyModel directly
        // via the expand/collapse flows; emit as raw bytes via a special wrapper
        void this._handleBinaryFrame(ev.data);
      }
    };

    ws.onclose = () => {
      // Frames still owed by the old socket never arrive.
      this._pendingExpands = [];
      if (!this.closed) {
        this._scheduleReconnect(wsUrl);
      }
    };

    ws.onerror = () => {
      // onclose fires next — reconnect handled there
    };
  }

  private _handleTextFrame(data: string): void {
    try {
      const event = JSON.parse(data) as TdpDeltaEvent;
      if (event.type === "batch_checkpoint") {
        this.lastCheckpointId = (event as BatchCheckpointEvent).seq;
      }
      for (const h of this.deltaHandlers) h(event);
    } catch (e) {
      console.error("[TDPClient] failed to parse text frame:", e);
    }
  }

  private async _handleBinaryFrame(data: ArrayBuffer | Blob): Promise<void> {
    try {
      const buf = data instanceof Blob ? await data.arrayBuffer() : data;
      const bytes = new Uint8Array(buf);

      // Decode once; the frame type is the schema's `schema_id` custom metadata (read from
      // the decoded schema, not by scanning bytes — review go-collector-ts-packages-07).
      let table: Table | null = null;
      try {
        table = tableFromIPC(bytes);
      } catch {
        table = null; // not an IPC stream
      }
      const schemaId = table?.schema.metadata.get("schema_id") ?? "";

      if (table && schemaId === "ads.v1.delta_event") {
        // A-2: TDP delta event — decode payload column and route as TdpDeltaEvent
        this._decodeDeltaEventFrame(table);
        return;
      }

      // subgraph_expand frames (nodes + edges) arrive as two consecutive binary
      // messages per request, in request order.
      this._routeSubgraphFrame(bytes, table);
    } catch (e) {
      console.error("[TDPClient] failed to handle binary frame:", e);
    }
  }

  /**
   * Assign a nodes/edges frame to the expand request it answers: by the frame's own
   * `cluster_id` column when it has a row (the ADS subgraph schemas carry it), else the
   * request already mid-way, else the oldest. A frame nobody asked for is ignored.
   */
  private _routeSubgraphFrame(bytes: Uint8Array, table: Table | null): void {
    if (this._pendingExpands.length === 0) return;
    const frameCluster = TDPClient._frameClusterId(table);
    const target =
      frameCluster !== null
        ? this._pendingExpands.find((p) => p.clusterId === frameCluster)
        : (this._pendingExpands.find((p) => p.nodes !== null) ?? this._pendingExpands[0]);
    if (!target) return;
    if (target.nodes === null) {
      target.nodes = bytes; // first frame: nodes IPC
      return;
    }
    // Second frame: edges IPC — complete the request.
    this._pendingExpands = this._pendingExpands.filter((p) => p !== target);
    for (const h of this.expandHandlers) h(target.clusterId, target.nodes, bytes);
  }

  private static _frameClusterId(table: Table | null): number | null {
    const column = table?.getChild("cluster_id");
    if (!table || !column || table.numRows === 0) return null;
    const v = column.get(0);
    return v === null || v === undefined ? null : Number(v);
  }

  /**
   * Dispatch an ADS v1 delta_event frame to deltaHandlers.
   *
   * The frame has two columns: event_type (utf8) and payload (binary, the event as UTF-8
   * JSON). The payload cell is read from the decoded column, not found by scanning the
   * buffer for braces: the scan started on a `{` byte inside the offsets buffer for about
   * 1 payload length in 256 and miscounted a brace inside a JSON string, silently dropping
   * those events (review go-collector-ts-packages-07).
   */
  private _decodeDeltaEventFrame(table: Table): void {
    try {
      const payload = table.getChild("payload");
      for (let row = 0; row < table.numRows; row++) {
        const cell = payload?.get(row) as Uint8Array | string | null | undefined;
        if (cell === null || cell === undefined) continue;
        this._handleTextFrame(typeof cell === "string" ? cell : new TextDecoder().decode(cell));
      }
    } catch (e) {
      console.error("[TDPClient] failed to decode delta_event Arrow frame:", e);
    }
  }

  private _scheduleReconnect(wsUrl: string): void {
    const jitter = Math.random() * 0.2 * this.backoffMs;
    const delay = Math.min(this.backoffMs + jitter, MAX_BACKOFF_MS);
    this.backoffMs = Math.min(this.backoffMs * 2, MAX_BACKOFF_MS);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.closed) this._connectWs(wsUrl);
    }, delay);
  }

  private _connectSse(sseUrl: string): void {
    const sse = new EventSource(sseUrl);
    this.sse = sse;

    sse.onmessage = (ev) => {
      try {
        const event = JSON.parse(ev.data as string) as MetricSummaryEvent;
        for (const h of this.metricHandlers) h(event);
      } catch (e) {
        console.error("[TDPClient] failed to parse SSE frame:", e);
      }
    };

    sse.onerror = () => {
      // EventSource reconnects automatically via Last-Event-ID
    };
  }
}
