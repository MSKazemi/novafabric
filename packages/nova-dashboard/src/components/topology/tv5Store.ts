/**
 * tv5Store — Zustand cross-panel state for the TV-5 3D topology view.
 *
 * Shared between TV5Panel, LODController, TimeSlider and any future
 * side-panels (node detail, cluster drill-down, etc.).
 */
import { create } from "zustand";

export interface CameraState {
  position: [number, number, number];
  target: [number, number, number];
}

interface TV5Store {
  /** ID of the currently selected (clicked) node, or null. */
  selectedNodeId: string | null;
  /** ID of the cluster that is currently "in focus" (expanded / centred). */
  focalClusterId: string | null;
  /** ID of the snapshot window currently displayed, or null (= live). */
  selectedWindowId: string | null;
  /** Last known OrbitControls camera state (position + look-at target). */
  cameraState: CameraState;

  /** Actions */
  setSelectedNode: (nodeId: string | null) => void;
  setFocalCluster: (clusterId: string | null) => void;
  setSelectedWindow: (windowId: string | null) => void;
  updateCamera: (
    position: [number, number, number],
    target: [number, number, number],
  ) => void;
}

export const useTV5Store = create<TV5Store>((set) => ({
  selectedNodeId: null,
  focalClusterId: null,
  selectedWindowId: null,
  cameraState: {
    position: [0, 0, 200],
    target: [0, 0, 0],
  },

  setSelectedNode: (nodeId) => set({ selectedNodeId: nodeId }),
  setFocalCluster: (clusterId) => set({ focalClusterId: clusterId }),
  setSelectedWindow: (windowId) => set({ selectedWindowId: windowId }),
  updateCamera: (position, target) =>
    set({ cameraState: { position, target } }),
}));
