/**
 * Reviews go-collector-ts-packages-07 and -14 — binary Arrow frames.
 *
 * 07: the delta-event payload was found by scanning the IPC bytes for `{`; a payload length
 * with a 0x7b byte (379, 635, …) started the scan inside the offsets buffer, and a brace
 * inside a JSON string broke the depth count, so those events were dropped silently.
 * 14: one pending-expand slot, reset by the next request, so overlapping expands assigned
 * A's frames to B, and a request sent on a closed socket still armed the slot.
 */
import { Binary, Int64, Schema, Table, Utf8, tableToIPC, vectorFromArray } from "apache-arrow";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TDPClient } from "../../src/tdp/client.js";

class MockWebSocket {
  static OPEN = 1;
  readyState = 1;
  sentMessages: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string | ArrayBuffer }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string, public protocols?: string[]) {}
  send(data: string): void { this.sentMessages.push(data); }
  close(): void {}
}

let ws: MockWebSocket;
let origWs: typeof WebSocket;
let origEs: typeof EventSource;

beforeEach(() => {
  origWs = globalThis.WebSocket;
  origEs = globalThis.EventSource;
  const Ctor = vi.fn(function (this: unknown, url: string, protocols?: string[]) {
    ws = new MockWebSocket(url, protocols);
    return ws;
  }) as unknown as typeof WebSocket;
  (Ctor as unknown as { OPEN: number }).OPEN = 1;
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = Ctor;
  (globalThis as unknown as { EventSource: unknown }).EventSource = vi.fn(function () {
    return { close() {}, onmessage: null, onerror: null };
  });
});

afterEach(() => {
  globalThis.WebSocket = origWs;
  globalThis.EventSource = origEs;
});

/** A frame shaped like `ads_encoder.encode_delta_event_arrow`. */
function deltaFrame(event: Record<string, unknown>): ArrayBuffer {
  const table = new Table({
    event_type: vectorFromArray([String(event.type)], new Utf8()),
    payload: vectorFromArray([new TextEncoder().encode(JSON.stringify(event))], new Binary()),
  });
  const withMeta = new Table(
    new Schema(table.schema.fields, new Map([["schema_id", "ads.v1.delta_event"]])),
    table.batches,
  );
  const ipc = tableToIPC(withMeta, "stream");
  return ipc.buffer.slice(ipc.byteOffset, ipc.byteOffset + ipc.byteLength) as ArrayBuffer;
}

/** A nodes/edges frame with one `cluster_id` row. */
function subgraphFrame(kind: string, clusterId: number): ArrayBuffer {
  const table = new Table({ cluster_id: vectorFromArray([BigInt(clusterId)], new Int64()) });
  const withMeta = new Table(
    new Schema(table.schema.fields, new Map([["schema_id", `ads.v1.${kind}`]])),
    table.batches,
  );
  const ipc = tableToIPC(withMeta, "stream");
  return ipc.buffer.slice(ipc.byteOffset, ipc.byteOffset + ipc.byteLength) as ArrayBuffer;
}

const settle = () => new Promise((r) => setTimeout(r, 0));

function connected(): TDPClient {
  const client = new TDPClient();
  client.connect("ws://x/topology/stream?token=t", "http://x/metrics");
  ws.onopen?.();
  return client;
}

describe("delta-event frames", () => {
  it("dispatches every event for payload lengths 100..700, including 379 and 635", async () => {
    const client = connected();
    const seen: unknown[] = [];
    client.onTopologyDelta((e) => seen.push(e));
    let sent = 0;
    for (let length = 100; length <= 700; length++) {
      const base = { type: "add_node", seq: length, node_id: "n" };
      const pad = length - JSON.stringify({ ...base, pad: "" }).length;
      ws.onmessage?.({ data: deltaFrame({ ...base, pad: "x".repeat(Math.max(pad, 0)) }) });
      sent++;
    }
    await settle();
    await vi.waitFor(() => expect(seen.length).toBe(sent));
    client.disconnect();
  });

  it("keeps an event whose JSON contains braces inside a string", async () => {
    const client = connected();
    const seen: Array<{ node_id?: string }> = [];
    client.onTopologyDelta((e) => seen.push(e as { node_id?: string }));
    ws.onmessage?.({ data: deltaFrame({ type: "add_node", seq: 1, node_id: "a}b{c}}" }) });
    await vi.waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0].node_id).toBe("a}b{c}}");
    client.disconnect();
  });
});

describe("subgraph expand", () => {
  it("gives each cluster its own frames when two expands overlap", async () => {
    const client = connected();
    const done: Array<[number, number]> = [];
    client.onSubgraphExpand((cid, nodes) => done.push([cid, nodes.byteLength]));
    client.sendExpandRequest(1);
    client.sendExpandRequest(2);
    for (const [kind, cid] of [["nodes", 1], ["edges", 1], ["nodes", 2], ["edges", 2]] as const) {
      ws.onmessage?.({ data: subgraphFrame(kind, cid) });
      await settle();
    }
    await vi.waitFor(() => expect(done.map(([cid]) => cid)).toEqual([1, 2]));
    client.disconnect();
  });

  it("arms no pending state for a request the closed socket never sent", async () => {
    const client = connected();
    ws.readyState = 3;
    client.sendExpandRequest(7);
    ws.readyState = 1;
    const done = vi.fn();
    client.onSubgraphExpand(done);
    ws.onmessage?.({ data: subgraphFrame("nodes", 99) });
    ws.onmessage?.({ data: subgraphFrame("edges", 99) });
    await settle();
    expect(done).not.toHaveBeenCalled();
    client.disconnect();
  });

  it("ignores a frame for a cluster nobody asked about", async () => {
    const client = connected();
    const done = vi.fn();
    client.onSubgraphExpand(done);
    client.sendExpandRequest(1);
    ws.onmessage?.({ data: subgraphFrame("nodes", 5) });
    ws.onmessage?.({ data: subgraphFrame("edges", 5) });
    await settle();
    expect(done).not.toHaveBeenCalled();
    client.disconnect();
  });
});
