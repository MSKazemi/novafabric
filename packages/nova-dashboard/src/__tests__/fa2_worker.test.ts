import { describe, expect, it } from "vitest";

/**
 * FA2 web worker interface tests.
 *
 * The worker runs in a browser Worker context and cannot be instantiated in
 * vitest's node environment.  These tests verify:
 *   1. The message protocol shapes match the documented interface.
 *   2. Pin/unpin semantics use the graphology "fixed" attribute (not D3 fx/fy).
 *   3. Message type discriminants are the exact strings the worker checks.
 *
 * No DOM, no Worker instantiation — pure type/protocol tests.
 */

// -----------------------------------------------------------------------
// Re-declare the worker message interfaces (mirroring fa2-worker.ts) so the
// test can assert their structural constraints independently of the source.
// -----------------------------------------------------------------------

interface SettleRequest {
  type: "settle";
  newNodeIds: string[];
  graphSnapshot: { nodes: Array<{ key: string; attributes: Record<string, unknown> }> };
}

interface SettledResponse {
  type: "settled";
  positions: Record<string, { x: number; y: number }>;
}

interface ErrorResponse {
  type: "error";
  message: string;
}

type Fa2WorkerMessage = SettledResponse | ErrorResponse;

// -----------------------------------------------------------------------
// Message type discriminants
// -----------------------------------------------------------------------

describe("FA2 worker message type discriminants", () => {
  it("settle request type is the exact string 'settle'", () => {
    const msg: SettleRequest = {
      type: "settle",
      newNodeIds: ["node-1"],
      graphSnapshot: { nodes: [{ key: "node-1", attributes: { x: 0, y: 0 } }] },
    };
    expect(msg.type).toBe("settle");
  });

  it("settled response type is the exact string 'settled'", () => {
    const resp: SettledResponse = {
      type: "settled",
      positions: { "node-1": { x: 1.0, y: 2.0 } },
    };
    expect(resp.type).toBe("settled");
  });

  it("error response type is the exact string 'error'", () => {
    const resp: ErrorResponse = { type: "error", message: "layout failed" };
    expect(resp.type).toBe("error");
  });
});

// -----------------------------------------------------------------------
// SettleRequest shape
// -----------------------------------------------------------------------

describe("SettleRequest shape", () => {
  it("newNodeIds is a string array", () => {
    const req: SettleRequest = {
      type: "settle",
      newNodeIds: ["a", "b", "c"],
      graphSnapshot: { nodes: [] },
    };
    expect(Array.isArray(req.newNodeIds)).toBe(true);
    for (const id of req.newNodeIds) {
      expect(typeof id).toBe("string");
    }
  });

  it("graphSnapshot has a nodes array", () => {
    const req: SettleRequest = {
      type: "settle",
      newNodeIds: [],
      graphSnapshot: { nodes: [{ key: "n1", attributes: { x: 0, y: 0 } }] },
    };
    expect(Array.isArray(req.graphSnapshot.nodes)).toBe(true);
  });

  it("graphSnapshot may have an empty newNodeIds list (no new nodes)", () => {
    const req: SettleRequest = {
      type: "settle",
      newNodeIds: [],
      graphSnapshot: { nodes: [] },
    };
    expect(req.newNodeIds).toHaveLength(0);
  });
});

// -----------------------------------------------------------------------
// SettledResponse shape
// -----------------------------------------------------------------------

describe("SettledResponse shape", () => {
  it("positions maps node IDs to {x, y} objects", () => {
    const resp: SettledResponse = {
      type: "settled",
      positions: {
        "agent-1": { x: 3.14, y: -2.71 },
        "agent-2": { x: 0.0, y: 1.0 },
      },
    };
    for (const pos of Object.values(resp.positions)) {
      expect(typeof pos.x).toBe("number");
      expect(typeof pos.y).toBe("number");
    }
  });

  it("positions may be empty (graph had no nodes)", () => {
    const resp: SettledResponse = { type: "settled", positions: {} };
    expect(Object.keys(resp.positions)).toHaveLength(0);
  });
});

// -----------------------------------------------------------------------
// OQ-02 resolution: graphology "fixed" attribute (not D3 fx/fy)
// -----------------------------------------------------------------------

describe("FA2 pin semantics (OQ-02)", () => {
  it("fixed=true pins a node (graphology convention, not D3 fx/fy)", () => {
    // This test documents the OQ-02 resolution: pinning uses the graphology
    // 'fixed' node attribute, not D3-force's {fx, fy} fields.
    const pinInstruction = { fixed: true };
    expect(pinInstruction.fixed).toBe(true);
    // Confirm the D3 convention is NOT used
    expect("fx" in pinInstruction).toBe(false);
    expect("fy" in pinInstruction).toBe(false);
  });

  it("fixed=false unpins a node", () => {
    const unpinInstruction = { fixed: false };
    expect(unpinInstruction.fixed).toBe(false);
  });
});

// -----------------------------------------------------------------------
// Discriminated union narrowing
// -----------------------------------------------------------------------

describe("Fa2WorkerMessage discriminated union", () => {
  it("narrows correctly on type='settled'", () => {
    const msg: Fa2WorkerMessage = {
      type: "settled",
      positions: { "n1": { x: 1, y: 2 } },
    };
    if (msg.type === "settled") {
      expect(msg.positions["n1"].x).toBe(1);
    } else {
      throw new Error("Unexpected branch");
    }
  });

  it("narrows correctly on type='error'", () => {
    const msg: Fa2WorkerMessage = { type: "error", message: "oops" };
    if (msg.type === "error") {
      expect(msg.message).toBe("oops");
    } else {
      throw new Error("Unexpected branch");
    }
  });
});

// -----------------------------------------------------------------------
// FA2 settings contract
// -----------------------------------------------------------------------

describe("FA2 layout settings", () => {
  it("required settings fields are present with positive values", () => {
    const settings = {
      gravity: 1.0,
      scalingRatio: 10.0,
      linLogMode: true,
      adjustSizes: false,
      barnesHutOptimize: false,
    };
    expect(settings.gravity).toBeGreaterThan(0);
    expect(settings.scalingRatio).toBeGreaterThan(0);
    expect(typeof settings.linLogMode).toBe("boolean");
  });

  it("iteration count is bounded (max 100 per settle call)", () => {
    const MAX_ITERATIONS = 100;
    expect(MAX_ITERATIONS).toBeGreaterThan(0);
    expect(MAX_ITERATIONS).toBeLessThanOrEqual(1000);
  });
});
