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
  private _pendingExpandClusterId: number | null = null;
  private _pendingNodesBatch: Uint8Array | null = null;

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
    this._pendingExpandClusterId = clusterId;
    this._pendingNodesBatch = null;
    this._sendMessage({ type: "subgraph_expand", cluster_id: clusterId });
  }

  sendCollapseRequest(clusterId: number): void {
    this._sendMessage({ type: "subgraph_collapse", cluster_id: clusterId });
  }

  // -----------------------------------------------------------------------
  // Private
  // -----------------------------------------------------------------------

  private _sendMessage(msg: TdpClientMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
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

      // Peek at the Arrow IPC schema_id metadata to distinguish frame types.
      // We decode the schema lazily: only attempt JSON parse for delta frames.
      const schemaId = this._peekArrowSchemaId(buf);

      if (schemaId === "ads.v1.delta_event") {
        // A-2: TDP delta event — decode payload column and route as TdpDeltaEvent
        this._decodeDeltaEventFrame(buf);
        return;
      }

      // subgraph_expand frames (nodes + edges) arrive in two consecutive binary
      // messages.  We accumulate them using the pending-expand state machine.
      if (this._pendingExpandClusterId !== null) {
        if (this._pendingNodesBatch === null) {
          // First binary frame: nodes IPC
          this._pendingNodesBatch = bytes;
        } else {
          // Second binary frame: edges IPC — dispatch to expand handlers
          const clusterId = this._pendingExpandClusterId;
          const nodesBatch = this._pendingNodesBatch;
          this._pendingExpandClusterId = null;
          this._pendingNodesBatch = null;
          for (const h of this.expandHandlers) h(clusterId, nodesBatch, bytes);
        }
      }
    } catch (e) {
      console.error("[TDPClient] failed to handle binary frame:", e);
    }
  }

  /**
   * Read the Arrow IPC stream header to extract the ``schema_id`` custom metadata
   * value without fully decoding the batch.  Returns an empty string on failure.
   *
   * Arrow IPC stream format layout (little-endian):
   *   - Magic + continuation markers vary by version, but the schema_id is encoded
   *     as a UTF-8 key in the schema's custom_metadata flatbuffer.  Rather than
   *     implementing a full flatbuffer parser we use a fast heuristic: search the
   *     first 4 KiB of bytes for the null-terminated UTF-8 string "schema_id" and
   *     read the value that follows it in the Arrow metadata encoding.
   *
   * This is intentionally heuristic — it covers all ADS v1 frames produced by
   * pyarrow's IPC stream writer.  A full flatbuffer decode is not needed because
   * we only need to distinguish three schema IDs.
   */
  private _peekArrowSchemaId(buf: ArrayBuffer): string {
    try {
      const view = new Uint8Array(buf, 0, Math.min(buf.byteLength, 4096));
      const key = "schema_id";
      const keyBytes = new TextEncoder().encode(key);
      // Search for the key string in the header region
      outer: for (let i = 0; i < view.length - keyBytes.length - 8; i++) {
        for (let k = 0; k < keyBytes.length; k++) {
          if (view[i + k] !== keyBytes[k]) continue outer;
        }
        // Found the key string. In Arrow flatbuffer metadata the key and value
        // are each stored as a 4-byte length-prefixed UTF-8 string.
        // Skip the key length prefix (4 bytes before 'i' pointing to first char)
        // and find the value length prefix after the key string.
        const afterKey = i + keyBytes.length;
        // Look for value length int32 LE in the next few bytes (within 8 bytes)
        for (let off = 0; off < 8 && afterKey + off + 4 <= view.length; off++) {
          const valLen =
            view[afterKey + off] |
            (view[afterKey + off + 1] << 8) |
            (view[afterKey + off + 2] << 16) |
            (view[afterKey + off + 3] << 24);
          if (valLen > 0 && valLen <= 128 && afterKey + off + 4 + valLen <= view.length) {
            const valBytes = view.slice(afterKey + off + 4, afterKey + off + 4 + valLen);
            const val = new TextDecoder().decode(valBytes);
            // Sanity check: ADS schema IDs start with "ads."
            if (val.startsWith("ads.")) return val;
          }
        }
      }
    } catch (_e) {
      // fall through
    }
    return "";
  }

  /**
   * Decode an ADS v1 delta_event Arrow IPC frame and dispatch to deltaHandlers.
   *
   * The frame has two columns: event_type (utf8) and payload (binary/JSON).
   * We extract the payload bytes, parse as JSON, and route identically to
   * _handleTextFrame so existing handlers need no changes.
   */
  private _decodeDeltaEventFrame(buf: ArrayBuffer): void {
    try {
      // Extract the payload column value from the Arrow IPC frame.
      // We use a heuristic scan: find the JSON object bytes embedded in the
      // binary payload column.  The payload is JSON-encoded so we look for
      // the outermost '{' ... '}' in the buffer's tail region.
      const view = new Uint8Array(buf);
      let start = -1;
      let depth = 0;
      for (let i = 0; i < view.length; i++) {
        const c = view[i];
        if (c === 0x7b /* '{' */ && start === -1) {
          start = i;
          depth = 1;
        } else if (start !== -1) {
          if (c === 0x7b) depth++;
          else if (c === 0x7d /* '}' */) {
            depth--;
            if (depth === 0) {
              const jsonBytes = view.slice(start, i + 1);
              const jsonStr = new TextDecoder().decode(jsonBytes);
              this._handleTextFrame(jsonStr);
              return;
            }
          }
        }
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
