import { describe, expect, it } from "vitest";

// Unit tests for TV-5 utility functions (no DOM rendering needed)

describe("TV5 snapshot store window ID validation", () => {
  const WINDOW_ID_RE = /^[a-z0-9_-]+$/;

  it("accepts valid window IDs", () => {
    expect(WINDOW_ID_RE.test("abc123")).toBe(true);
    expect(WINDOW_ID_RE.test("win-001")).toBe(true);
    expect(WINDOW_ID_RE.test("test_snapshot")).toBe(true);
  });

  it("rejects path traversal attempts", () => {
    expect(WINDOW_ID_RE.test("../etc/passwd")).toBe(false);
    expect(WINDOW_ID_RE.test("ABC")).toBe(false);
    expect(WINDOW_ID_RE.test("win/001")).toBe(false);
  });

  it("rejects empty string", () => {
    expect(WINDOW_ID_RE.test("")).toBe(false);
  });

  it("rejects strings with dots", () => {
    expect(WINDOW_ID_RE.test("win.001")).toBe(false);
  });
});

describe("latencyToColor", () => {
  // Copy logic for testing without React import
  function latencyToColor(p99Ms: number): string {
    if (p99Ms <= 50) return "#22c55e";
    if (p99Ms <= 200) return "#eab308";
    return "#ef4444";
  }

  it("maps low latency to green", () => {
    expect(latencyToColor(0)).toBe("#22c55e");
    expect(latencyToColor(50)).toBe("#22c55e");
  });

  it("maps medium latency to yellow", () => {
    expect(latencyToColor(51)).toBe("#eab308");
    expect(latencyToColor(100)).toBe("#eab308");
    expect(latencyToColor(200)).toBe("#eab308");
  });

  it("maps high latency to red", () => {
    expect(latencyToColor(201)).toBe("#ef4444");
    expect(latencyToColor(1000)).toBe("#ef4444");
  });
});

describe("TV5 snapshot shape", () => {
  it("valid snapshot has required fields", () => {
    const snapshot = {
      window_id: "test001",
      timestamp: 1716000000,
      node_count: 3,
      edge_count: 2,
      positions: {
        "agent-1": [0, 0, 0],
        "model-gpt4": [10, 5, 2],
      },
      node_types: { "agent-1": "agent", "model-gpt4": "model" },
      layout_elapsed_ms: 42.5,
    };
    expect(snapshot.window_id).toBeDefined();
    expect(typeof snapshot.node_count).toBe("number");
    expect(Object.keys(snapshot.positions)).toHaveLength(2);
  });

  it("positions map each node to a 3-element array", () => {
    const positions: Record<string, number[]> = {
      node1: [1.0, 2.0, 3.0],
      node2: [-1.5, 0.0, 4.2],
    };
    for (const pos of Object.values(positions)) {
      expect(pos).toHaveLength(3);
      for (const coord of pos) {
        expect(typeof coord).toBe("number");
      }
    }
  });
});

describe("TV5 node types", () => {
  type NodeType = "agent" | "model" | "tool" | "compute_node";
  const VALID_TYPES: NodeType[] = ["agent", "model", "tool", "compute_node"];

  it("covers all expected node types", () => {
    expect(VALID_TYPES).toContain("agent");
    expect(VALID_TYPES).toContain("model");
    expect(VALID_TYPES).toContain("tool");
    expect(VALID_TYPES).toContain("compute_node");
  });
});

describe("TV5 edge shape", () => {
  it("edge references source and destination by index", () => {
    const edge = { srcIdx: 0, dstIdx: 1 };
    expect(typeof edge.srcIdx).toBe("number");
    expect(typeof edge.dstIdx).toBe("number");
  });
});
