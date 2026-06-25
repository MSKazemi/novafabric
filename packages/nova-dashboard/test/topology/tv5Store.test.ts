/**
 * Tests for the TV-5 Zustand cross-panel state store.
 * Pure logic tests — no React/DOM needed.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useTV5Store } from "../../src/components/topology/tv5Store.js";

// Reset store state between tests to prevent leakage
beforeEach(() => {
  useTV5Store.setState({
    selectedNodeId: null,
    focalClusterId: null,
    selectedWindowId: null,
    cameraState: { position: [0, 0, 200], target: [0, 0, 0] },
  });
});

describe("useTV5Store — initial state", () => {
  it("starts with no selected node", () => {
    const { selectedNodeId } = useTV5Store.getState();
    expect(selectedNodeId).toBeNull();
  });

  it("starts with no focal cluster", () => {
    const { focalClusterId } = useTV5Store.getState();
    expect(focalClusterId).toBeNull();
  });

  it("starts with no selected window (live mode)", () => {
    const { selectedWindowId } = useTV5Store.getState();
    expect(selectedWindowId).toBeNull();
  });

  it("starts with default camera position", () => {
    const { cameraState } = useTV5Store.getState();
    expect(cameraState.position).toEqual([0, 0, 200]);
    expect(cameraState.target).toEqual([0, 0, 0]);
  });
});

describe("useTV5Store — setSelectedNode", () => {
  it("sets a node ID", () => {
    useTV5Store.getState().setSelectedNode("agent-42");
    expect(useTV5Store.getState().selectedNodeId).toBe("agent-42");
  });

  it("clears a node ID", () => {
    useTV5Store.getState().setSelectedNode("agent-42");
    useTV5Store.getState().setSelectedNode(null);
    expect(useTV5Store.getState().selectedNodeId).toBeNull();
  });
});

describe("useTV5Store — setFocalCluster", () => {
  it("sets a focal cluster", () => {
    useTV5Store.getState().setFocalCluster("cluster-A");
    expect(useTV5Store.getState().focalClusterId).toBe("cluster-A");
  });

  it("clears the focal cluster", () => {
    useTV5Store.getState().setFocalCluster("cluster-A");
    useTV5Store.getState().setFocalCluster(null);
    expect(useTV5Store.getState().focalClusterId).toBeNull();
  });
});

describe("useTV5Store — setSelectedWindow", () => {
  it("sets a window ID (enters history mode)", () => {
    useTV5Store.getState().setSelectedWindow("win-001");
    expect(useTV5Store.getState().selectedWindowId).toBe("win-001");
  });

  it("clears window ID (returns to live mode)", () => {
    useTV5Store.getState().setSelectedWindow("win-001");
    useTV5Store.getState().setSelectedWindow(null);
    expect(useTV5Store.getState().selectedWindowId).toBeNull();
  });
});

describe("useTV5Store — updateCamera", () => {
  it("updates position and target", () => {
    useTV5Store
      .getState()
      .updateCamera([10, 20, 30], [1, 2, 3]);
    const { cameraState } = useTV5Store.getState();
    expect(cameraState.position).toEqual([10, 20, 30]);
    expect(cameraState.target).toEqual([1, 2, 3]);
  });

  it("can set camera back to origin", () => {
    useTV5Store.getState().updateCamera([50, 50, 50], [5, 5, 5]);
    useTV5Store.getState().updateCamera([0, 0, 0], [0, 0, 0]);
    const { cameraState } = useTV5Store.getState();
    expect(cameraState.position).toEqual([0, 0, 0]);
  });
});

describe("useTV5Store — state independence", () => {
  it("node, cluster and window selections are independent", () => {
    useTV5Store.getState().setSelectedNode("n1");
    useTV5Store.getState().setFocalCluster("c1");
    useTV5Store.getState().setSelectedWindow("w1");

    const state = useTV5Store.getState();
    expect(state.selectedNodeId).toBe("n1");
    expect(state.focalClusterId).toBe("c1");
    expect(state.selectedWindowId).toBe("w1");
  });
});
