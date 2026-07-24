/**
 * Zero-dependency client-side chart image export (ADR-0201).
 *
 * The dashboard charts are plain inline SVG styled with CSS custom properties
 * (theme tokens). A naive `XMLSerializer` snapshot would lose every `var(--…)`
 * reference once the markup leaves the page, so `serializeSvg` walks the live
 * element tree and inlines the COMPUTED values of the presentation properties
 * onto a detached clone — resolving CSS vars, `currentColor`, and the active
 * light/dark theme to concrete colors. PNG export rasterizes that
 * self-contained SVG through an offscreen canvas; because the markup is
 * inline-only (no external refs), the canvas is never tainted.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Presentation properties whose computed values are inlined on export. */
const INLINE_PROPS = [
  'fill',
  'stroke',
  'stroke-width',
  'opacity',
  'font-family',
  'font-size',
  'text-anchor',
] as const;

/**
 * Resolve the effective background behind `el` so PNG exports are never
 * transparent: nearest non-transparent ancestor background, falling back to
 * the card-surface token (`--color-bg-raised`), then to the dark default.
 */
function resolveBackground(el: SVGSVGElement): string {
  let node: Element | null = el.parentElement;
  while (node) {
    const bg = window.getComputedStyle(node).backgroundColor;
    if (bg && bg !== 'transparent' && bg !== 'rgba(0, 0, 0, 0)') return bg;
    node = node.parentElement;
  }
  const token = window
    .getComputedStyle(document.documentElement)
    .getPropertyValue('--color-bg-raised')
    .trim();
  return token || '#111114';
}

/**
 * Serialize an inline SVG to a standalone XML string with all theme-dependent
 * presentation styles inlined at their current computed values.
 *
 * - deep-clones the element, so the live DOM is untouched;
 * - inlines computed fill / stroke / stroke-width / opacity / font-family /
 *   font-size / text-anchor per element (resolves `var(--…)` + `currentColor`);
 * - sets `xmlns` and explicit pixel `width`/`height` from the viewBox
 *   (multiplied by `opts.scale`, default 1);
 * - prepends an opaque background `<rect>` matching the page/card surface.
 */
export function serializeSvg(el: SVGSVGElement, opts: { scale?: number } = {}): string {
  const scale = opts.scale ?? 1;
  const clone = el.cloneNode(true) as SVGSVGElement;

  // Pair each source element with its clone (identical traversal order) and
  // inline the computed presentation values. Computed style must come from
  // the *live* element — a detached clone has no computed style.
  const sources = [el as Element, ...Array.from(el.querySelectorAll('*'))];
  const clones = [clone as Element, ...Array.from(clone.querySelectorAll('*'))];
  for (let i = 0; i < sources.length; i++) {
    const src = sources[i];
    const dst = clones[i];
    if (!(dst instanceof SVGElement) && !(dst instanceof HTMLElement)) continue;
    const computed = window.getComputedStyle(src);
    for (const prop of INLINE_PROPS) {
      const value = computed.getPropertyValue(prop);
      if (value) dst.style.setProperty(prop, value);
    }
    // Class names are meaningless without the page stylesheet.
    dst.removeAttribute('class');
  }

  // Explicit dimensions from the viewBox (falling back to the rendered size).
  const vb = el.viewBox.baseVal;
  const hasViewBox = !!vb && vb.width > 0 && vb.height > 0;
  const vbX = hasViewBox ? vb.x : 0;
  const vbY = hasViewBox ? vb.y : 0;
  const vbW = hasViewBox ? vb.width : el.clientWidth || 300;
  const vbH = hasViewBox ? vb.height : el.clientHeight || 150;
  clone.setAttribute('xmlns', SVG_NS);
  clone.setAttribute('width', String(Math.round(vbW * scale)));
  clone.setAttribute('height', String(Math.round(vbH * scale)));
  if (!clone.getAttribute('viewBox')) {
    clone.setAttribute('viewBox', `${vbX} ${vbY} ${vbW} ${vbH}`);
  }

  // Opaque background so PNG exports are never transparent.
  const bgRect = document.createElementNS(SVG_NS, 'rect');
  bgRect.setAttribute('x', String(vbX));
  bgRect.setAttribute('y', String(vbY));
  bgRect.setAttribute('width', String(vbW));
  bgRect.setAttribute('height', String(vbH));
  bgRect.setAttribute('fill', resolveBackground(el));
  clone.insertBefore(bgRect, clone.firstChild);

  return new XMLSerializer().serializeToString(clone);
}

/** Blob + anchor-click download (same idiom as the ReportsTab CSV download). */
function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** Download the chart as a standalone `.svg` file at the active theme. */
export function downloadSvg(el: SVGSVGElement, filename: string): void {
  const markup = serializeSvg(el);
  triggerDownload(new Blob([markup], { type: 'image/svg+xml' }), filename);
}

/**
 * Download the chart as a `.png` at `scale`× the viewBox resolution (default
 * 2×): serialized SVG → Image → offscreen canvas → `canvas.toBlob`.
 */
export function downloadPng(el: SVGSVGElement, filename: string, scale = 2): Promise<void> {
  const markup = serializeSvg(el, { scale });
  const svgUrl = URL.createObjectURL(new Blob([markup], { type: 'image/svg+xml' }));
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(svgUrl);
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext('2d');
      if (!ctx) {
        reject(new Error('chart export: 2d canvas context unavailable'));
        return;
      }
      ctx.drawImage(img, 0, 0);
      canvas.toBlob((blob) => {
        if (!blob) {
          reject(new Error('chart export: PNG encoding failed'));
          return;
        }
        triggerDownload(blob, filename);
        resolve();
      }, 'image/png');
    };
    img.onerror = () => {
      URL.revokeObjectURL(svgUrl);
      reject(new Error('chart export: SVG rasterization failed'));
    };
    img.src = svgUrl;
  });
}
