/**
 * SigmaRenderer — WebGL2 Sigma.js wrapper with click-to-expand and
 * partial-graph refresh (OQ-03 resolved: partialGraph + skipIndexation).
 *
 * Readability features:
 *  - label declutter: only large nodes label by default; hovered/selected
 *    nodes always show their label (node reducer + forceLabel).
 *  - zoom controls: zoomIn / zoomOut / fit (used by on-screen +/-/Fit buttons).
 *  - layered layout swap: applyLayeredLayout()/restoreForceLayout() let the
 *    Call-graph view reuse this same renderer with dagre positions.
 */

import Sigma from "sigma";
import { NodeCircleProgram } from "sigma/rendering";
import type { GraphologyModel } from "../graph/model.js";
import { computeLayeredLayout } from "../graph/layoutDagre.js";
import { computeFitRatio } from "./fit.js";

export type NodeClickHandler = (clusterId: number | null, nodeId: string) => void;

// Below this on-screen size, labels are hidden unless the node is hovered or
// selected. Higher than the old default (8) so the full-graph view is legible.
const LABEL_SIZE_THRESHOLD = 14;


export class SigmaRenderer {
  private _sigma: Sigma;
  private _model: GraphologyModel;
  private _onNodeClick: NodeClickHandler | null = null;
  private _hoveredNode: string | null = null;
  private _selectedNode: string | null = null;
  // Saved force/centroid positions, captured before switching to layered layout.
  private _savedLayout: Map<string, { x: number; y: number }> | null = null;

  constructor(container: HTMLElement, model: GraphologyModel) {
    this._model = model;
    this._sigma = new Sigma(model.graph, container, {
      renderEdgeLabels: false,
      labelRenderedSizeThreshold: LABEL_SIZE_THRESHOLD,
      defaultNodeColor: "#6366f1",
      nodeProgramClasses: {
        cluster: NodeCircleProgram,
        agent: NodeCircleProgram,
        model: NodeCircleProgram,
      },
      nodeReducer: (node, data) => this._reduceNode(node, data),
    });

    this._sigma.on("clickNode", ({ node }) => {
      this._selectedNode = node;
      const type = model.graph.getNodeAttribute(node, "type") as string | undefined;
      const clusterId = model.graph.getNodeAttribute(node, "cluster_id") as number | undefined;
      this._onNodeClick?.(type === "cluster" ? (clusterId ?? null) : null, node);
      this._sigma.refresh();
    });

    // Hover: highlight + force-show the label of the node under the cursor.
    this._sigma.on("enterNode", ({ node }) => {
      this._hoveredNode = node;
      this._sigma.refresh();
    });
    this._sigma.on("leaveNode", () => {
      this._hoveredNode = null;
      this._sigma.refresh();
    });

    // Disable double-click zoom on background; single background click zooms to fit.
    this._sigma.on("doubleClickStage", (e) => e.preventSigmaDefault());
    this._sigma.on("clickStage", () => {
      this._selectedNode = null;
      this._sigma.getCamera().animatedReset();
    });

    // Register refresh callback so model updates trigger redraws.
    model.setRefreshCallback((newNodeIds) => this._refresh(newNodeIds));
  }

  setNodeClickHandler(handler: NodeClickHandler): void {
    this._onNodeClick = handler;
  }

  // ---- Zoom controls (wired to on-screen +/-/Fit buttons) ----

  zoomIn(): void {
    this._sigma.getCamera().animatedZoom({ duration: 200 });
  }

  zoomOut(): void {
    this._sigma.getCamera().animatedUnzoom({ duration: 200 });
  }

  /** Frame the whole graph, including node radii.
   *
   * `animatedReset()` alone returns the camera to ratio 1, which frames the
   * normalised [0,1] coordinate space — but NOT the nodes drawn in it. Node
   * `size` is in screen pixels, so a large cluster sitting near the edge of
   * that space is drawn half outside the viewport and looks clipped (a
   * 106-agent cluster renders at ~82px radius). Zoom out by the largest node
   * radius, expressed as a fraction of the smaller viewport dimension, plus a
   * small margin so labels are not flush against the edge.
   */
  fit(): void {
    const camera = this._sigma.getCamera();
    const graph = this._model.graph;
    if (graph.order === 0) {
      camera.animatedReset({ duration: 300 });
      return;
    }

    let maxSize = 0;
    graph.forEachNode((_id, attrs) => {
      const size = (attrs.size as number) ?? 0;
      if (size > maxSize) maxSize = size;
    });

    const { width, height } = this._sigma.getDimensions();
    camera.animate(
      { x: 0.5, y: 0.5, ratio: computeFitRatio(maxSize, width, height), angle: 0 },
      { duration: 300 },
    );
  }

  // ---- Layered (call-graph) layout swap ----

  /** Apply a dagre layered layout over the current graph, saving the prior
   *  positions so restoreForceLayout() can put them back. */
  applyLayeredLayout(): void {
    const g = this._model.graph;
    if (!this._savedLayout) {
      this._savedLayout = new Map();
      g.forEachNode((id, attrs) => {
        this._savedLayout!.set(id, {
          x: (attrs.x as number) ?? 0,
          y: (attrs.y as number) ?? 0,
        });
      });
    }
    const nodes = g.mapNodes((id, attrs) => ({ id, size: attrs.size as number }));
    const edges = g.mapEdges((_e, _a, source, target) => ({ source, target }));
    const pos = computeLayeredLayout(nodes, edges, { rankdir: "TB" });
    for (const [id, p] of pos) {
      if (g.hasNode(id)) {
        g.setNodeAttribute(id, "x", p.x);
        g.setNodeAttribute(id, "y", p.y);
      }
    }
    this._sigma.refresh();
    this.fit();
  }

  /** Restore the force/centroid positions captured before applyLayeredLayout(). */
  restoreForceLayout(): void {
    if (!this._savedLayout) return;
    const g = this._model.graph;
    for (const [id, p] of this._savedLayout) {
      if (g.hasNode(id)) {
        g.setNodeAttribute(id, "x", p.x);
        g.setNodeAttribute(id, "y", p.y);
      }
    }
    this._savedLayout = null;
    this._sigma.refresh();
    this.fit();
  }

  destroy(): void {
    this._sigma.kill();
  }

  // ---- internals ----

  private _reduceNode(
    node: string,
    data: { [k: string]: unknown },
  ): { [k: string]: unknown } {
    const isFocus = node === this._hoveredNode || node === this._selectedNode;
    if (isFocus) {
      return { ...data, highlighted: true, forceLabel: true, zIndex: 1 };
    }
    return data;
  }

  private _refresh(newNodeIds?: string[]): void {
    if (newNodeIds && newNodeIds.length > 0) {
      // OQ-03: partial refresh — only re-index the new nodes, skip full canvas redraw.
      this._sigma.refresh({
        partialGraph: { nodes: newNodeIds },
        skipIndexation: true,
      });
    } else {
      this._sigma.refresh();
    }
  }
}
