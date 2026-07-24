/**
 * Camera framing math for the topology graph.
 *
 * Deliberately a separate module from `renderer.ts`: that file imports
 * `sigma`, which touches `WebGL2RenderingContext` at module-load time and so
 * cannot be imported under vitest's node environment. Keeping the math here
 * makes it directly testable.
 */

/** Extra breathing room on top of the largest node's diameter, so labels are
 *  not flush against the viewport edge. */
export const FIT_MARGIN = 0.15;

/**
 * Camera ratio that frames the normalised [0,1] node space *plus* node radii.
 *
 * Sigma normalises node positions into [0,1] but draws `size` in screen
 * pixels, so framing the coordinate space alone clips large nodes sitting near
 * its edge — a 106-agent cluster renders at ~82px radius and lands half
 * outside the viewport. Zooming out by that node's diameter, expressed as a
 * fraction of the *smaller* viewport dimension, guarantees every node is fully
 * on screen in both portrait and landscape windows.
 *
 * Never returns < 1: fit() must not zoom in past the full extent.
 */
export function computeFitRatio(
  maxNodeSize: number,
  width: number,
  height: number,
): number {
  const shortest = Math.max(1, Math.min(width, height));
  const radiusFraction = Math.max(0, maxNodeSize) / shortest;
  return 1 + 2 * radiusFraction + FIT_MARGIN;
}
