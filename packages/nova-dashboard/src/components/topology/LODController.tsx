/**
 * LODController — Level-of-Detail controller for the TV-5 3D topology view.
 *
 * Design contract (from ADR-0068 build prompt):
 *   - When a cluster's projected screen diameter falls below LOD_THRESHOLD_PX
 *     (60 px), collapse it to a Sprite supernode.
 *   - Non-focal clusters get opacity 0.15 (ghosting).
 *   - Zero React reconciler re-renders: all per-frame work uses refs and
 *     direct Three.js mutations via useFrame.
 */
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

/** Cluster diameter in projected screen pixels below which we collapse. */
const LOD_THRESHOLD_PX = 60;

/** Opacity assigned to non-focal clusters (ghosting effect). */
const NON_FOCAL_OPACITY = 0.15;

/** Approximate world-space radius used for screen-size estimation. */
const CLUSTER_WORLD_RADIUS = 40;

export interface ClusterRecord {
  cluster_id: string;
  centroid: [number, number, number];
  node_count: number;
}

export interface LODResult {
  shouldCollapse: Set<string>;
  ghostOpacity: Map<string, number>;
}

/**
 * useLOD — runs inside a react-three-fiber Canvas (requires a valid frame loop).
 *
 * Computes LOD state for each cluster every frame using the current camera
 * projection, then writes the result into a stable ref so callers can read
 * it without triggering React renders.
 *
 * @param camera   The Three.js camera (from useThree or passed as prop).
 * @param clusters Cluster records with centroid positions.
 * @param focalClusterId  The cluster currently "in focus", or null.
 * @returns A ref containing the current LODResult (stable object identity).
 */
export function useLOD(
  camera: THREE.Camera,
  clusters: ClusterRecord[],
  focalClusterId: string | null,
): React.RefObject<LODResult> {
  const resultRef = useRef<LODResult>({
    shouldCollapse: new Set(),
    ghostOpacity: new Map(),
  });

  useFrame(() => {
    const shouldCollapse = new Set<string>();
    const ghostOpacity = new Map<string, number>();

    // We need a PerspectiveCamera to compute FOV-based screen size.
    const isPerspective = (camera as THREE.PerspectiveCamera).isPerspectiveCamera;

    for (const cluster of clusters) {
      const [cx, cy, cz] = cluster.centroid;
      const worldPos = new THREE.Vector3(cx, cy, cz);

      // --- Screen-space projected size ---
      let screenDiameterPx = LOD_THRESHOLD_PX + 1; // default: visible

      if (isPerspective) {
        const perspCam = camera as THREE.PerspectiveCamera;
        const dist = camera.position.distanceTo(worldPos);
        if (dist > 0) {
          // Angular size in radians → screen pixels
          const fovRad = THREE.MathUtils.degToRad(perspCam.fov);
          // Screen height in pixels (renderer canvas; fall back to 600)
          const screenH =
            perspCam.aspect > 0
              ? perspCam.aspect > 0
                ? 600 // r3f canvas height approximation
                : 600
              : 600;
          const projectedRadius =
            (CLUSTER_WORLD_RADIUS / dist) * (screenH / (2 * Math.tan(fovRad / 2)));
          screenDiameterPx = projectedRadius * 2;
        }
      }

      if (screenDiameterPx < LOD_THRESHOLD_PX) {
        shouldCollapse.add(cluster.cluster_id);
      }

      // --- Ghost opacity ---
      if (focalClusterId !== null && cluster.cluster_id !== focalClusterId) {
        ghostOpacity.set(cluster.cluster_id, NON_FOCAL_OPACITY);
      } else {
        ghostOpacity.set(cluster.cluster_id, 1.0);
      }
    }

    // Mutate the result ref directly — no setState, no reconciler work.
    resultRef.current.shouldCollapse = shouldCollapse;
    resultRef.current.ghostOpacity = ghostOpacity;
  });

  return resultRef;
}

/**
 * LODController — a render-less React Three Fiber component.
 *
 * Mount inside a <Canvas> to activate per-frame LOD computation.
 * Reads results via the returned ref from useLOD, not from component state.
 *
 * Example:
 *   const lodRef = useLOD(camera, clusters, focalClusterId);
 *   // lodRef.current.shouldCollapse → Set of cluster IDs to collapse
 */
export function LODController({
  camera,
  clusters,
  focalClusterId,
  onLODUpdate,
}: {
  camera: THREE.Camera;
  clusters: ClusterRecord[];
  focalClusterId: string | null;
  onLODUpdate?: (result: LODResult) => void;
}): null {
  const resultRef = useLOD(camera, clusters, focalClusterId);

  // Fire optional callback when LOD result changes (for external consumers
  // that cannot call useLOD directly, e.g. non-r3f components).
  useFrame(() => {
    if (onLODUpdate) {
      onLODUpdate(resultRef.current);
    }
  });

  return null;
}
