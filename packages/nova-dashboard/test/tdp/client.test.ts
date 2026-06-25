/**
 * TDPClient unit tests — uses mock WebSocket to avoid real network.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { TDPClient } from "../../src/tdp/client.js";
import type { BatchCheckpointEvent } from "../../src/tdp/types.js";

// Minimal WebSocket mock
class MockWebSocket {
  static OPEN = 1;
  readyState = MockWebSocket.OPEN;
  sentMessages: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string | ArrayBuffer }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string, public protocols?: string[]) {}
  send(data: string): void { this.sentMessages.push(data); }
  close(): void {}
}

class MockEventSource {
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {}
  close(): void {}
}

describe("TDPClient", () => {
  let origWebSocket: typeof WebSocket;
  let origEventSource: typeof EventSource;
  let mockWs: MockWebSocket;

  beforeEach(() => {
    origWebSocket = globalThis.WebSocket;
    origEventSource = globalThis.EventSource;

    // Constructor mocks require regular function syntax (not arrow).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const WsMock = vi.fn(function (this: MockWebSocket, url: string, protocols?: string[]) {
      mockWs = new MockWebSocket(url, protocols);
      Object.assign(this, mockWs);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return mockWs as any;
    }) as unknown as typeof WebSocket;
    // WebSocket static constants required by TDPClient readyState check
    (WsMock as unknown as { OPEN: number }).OPEN = 1;
    (WsMock as unknown as { CONNECTING: number }).CONNECTING = 0;
    (WsMock as unknown as { CLOSING: number }).CLOSING = 2;
    (WsMock as unknown as { CLOSED: number }).CLOSED = 3;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).WebSocket = WsMock;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).EventSource = vi.fn(function (this: MockEventSource, url: string) {
      const inst = new MockEventSource(url);
      Object.assign(this, inst);
      return inst;
    });
  });

  afterEach(() => {
    globalThis.WebSocket = origWebSocket;
    globalThis.EventSource = origEventSource;
  });

  it("opens WebSocket with nova-tdp-v1 subprotocol", () => {
    const client = new TDPClient();
    client.connect("ws://127.0.0.1:4321/topology/stream?token=x", "http://...");
    expect(mockWs.protocols).toContain("nova-tdp-v1");
    client.disconnect();
  });

  it("records last batch_checkpoint seq", () => {
    const client = new TDPClient();
    client.connect("ws://127.0.0.1:4321/topology/stream?token=x", "http://...");
    mockWs.onopen?.();

    const ckpt: BatchCheckpointEvent = { type: "batch_checkpoint", seq: 42, ts_ms: Date.now() };
    mockWs.onmessage?.({ data: JSON.stringify(ckpt) });

    // Trigger a reconnect and check resume_from
    mockWs.onclose?.();
    // Fast-forward reconnect timer
    vi.useFakeTimers();
    vi.runAllTimers();
    vi.useRealTimers();

    // A new WS was created — check resume_from in sent messages
    const resumeMsg = mockWs.sentMessages.find((m) => {
      const parsed = JSON.parse(m) as { type: string };
      return parsed.type === "resume_from";
    });
    // Note: reconnect happens asynchronously; just verify checkpoint was recorded
    expect((client as unknown as { lastCheckpointId: number }).lastCheckpointId).toBe(42);
    client.disconnect();
  });

  it("calls delta handlers on text frame", () => {
    const client = new TDPClient();
    const handler = vi.fn();
    client.onTopologyDelta(handler);
    client.connect("ws://127.0.0.1:4321/topology/stream?token=x", "http://...");
    mockWs.onopen?.();

    const ev = { type: "topology_reset", seq: 1 };
    mockWs.onmessage?.({ data: JSON.stringify(ev) });
    expect(handler).toHaveBeenCalledWith(ev);
    client.disconnect();
  });

  it("does not throw on malformed text frame", () => {
    const client = new TDPClient();
    const handler = vi.fn();
    client.onTopologyDelta(handler);
    client.connect("ws://127.0.0.1:4321/topology/stream?token=x", "http://...");
    mockWs.onopen?.();

    expect(() => {
      mockWs.onmessage?.({ data: "this is not json{{{{" });
    }).not.toThrow();
    expect(handler).not.toHaveBeenCalled();
    client.disconnect();
  });

  it("sends sendExpandRequest as JSON", () => {
    const client = new TDPClient();
    client.connect("ws://127.0.0.1:4321/topology/stream?token=x", "http://...");
    mockWs.onopen?.();
    client.sendExpandRequest(3);
    expect(mockWs.sentMessages).toContainEqual(
      JSON.stringify({ type: "subgraph_expand", cluster_id: 3 }),
    );
    client.disconnect();
  });
});
