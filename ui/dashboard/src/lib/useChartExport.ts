/**
 * Hook wiring a chart container to the zero-dependency image exporters
 * (ADR-0201). Lives in `src/lib/` alongside the other hooks (`usePolling`,
 * `useUrlState`, `useMutation`) — this repo has no `src/hooks/` directory.
 */
import { useCallback, useRef } from 'react';
import { downloadPng, downloadSvg } from './chartExport';

export interface ChartExport {
  /** Attach to the element wrapping the chart; the first `<svg>` under it is exported. */
  containerRef: React.RefObject<HTMLDivElement | null>;
  exportSvg: () => void;
  exportPng: () => void;
}

/**
 * `filename` is the base name without extension — `.svg` / `.png` is appended.
 * Both exporters no-op gracefully when no `<svg>` is present under the ref.
 */
export function useChartExport(filename: string): ChartExport {
  const containerRef = useRef<HTMLDivElement | null>(null);

  const findSvg = useCallback(
    () => containerRef.current?.querySelector('svg') ?? null,
    [],
  );

  const exportSvg = useCallback(() => {
    const svg = findSvg();
    if (!svg) return;
    downloadSvg(svg, `${filename}.svg`);
  }, [filename, findSvg]);

  const exportPng = useCallback(() => {
    const svg = findSvg();
    if (!svg) return;
    // Failures (e.g. canvas unavailable) are non-fatal for a download affordance.
    void downloadPng(svg, `${filename}.png`).catch(() => undefined);
  }, [filename, findSvg]);

  return { containerRef, exportSvg, exportPng };
}
