import type { ReactNode } from 'react';
import { useChartExport } from '../../lib/useChartExport';
import Badge from './primitives/Badge';
import Skeleton from './Skeleton';

/**
 * Card wrapper for dashboard SVG charts (matches the AnalyticsTab chart-card
 * markup) with a compact SVG / PNG image-export affordance (ADR-0201).
 * Exports resolve theme tokens to concrete colors at the active theme.
 */
export default function ChartCard({
  title,
  legend,
  filename,
  loading = false,
  /** Mark the chart as computed from an approximate/cached aggregate. */
  approximate = false,
  children,
}: {
  title: string;
  legend?: ReactNode;
  /** Download base name (no extension). Defaults to a slug of `title`. */
  filename?: string;
  loading?: boolean;
  approximate?: boolean;
  children: ReactNode;
}) {
  const base = filename ?? title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const { containerRef, exportSvg, exportPng } = useChartExport(base);

  const exportBtnClass =
    'font-mono text-[var(--text-2xs)] uppercase tracking-wider text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:underline underline-offset-2';

  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-[var(--text-2xs)] font-mono uppercase tracking-wider text-[var(--color-text-faint)] truncate">
            {title}
          </span>
          {approximate && <Badge tone="pending">approx</Badge>}
        </span>
        <span className="flex items-center gap-3 shrink-0">
          {legend}
          {!loading && (
            <span className="flex items-center gap-1 text-[var(--text-2xs)] font-mono text-[var(--color-text-faint)]">
              <span aria-hidden="true">⬇</span>
              <button type="button" onClick={exportSvg} title={`Download “${title}” as SVG`} className={exportBtnClass}>
                SVG
              </button>
              <span aria-hidden="true">/</span>
              <button type="button" onClick={exportPng} title={`Download “${title}” as PNG`} className={exportBtnClass}>
                PNG
              </button>
            </span>
          )}
        </span>
      </div>
      {loading ? (
        <div className="space-y-2 py-2" aria-label="Loading chart">
          <Skeleton height="h-3" width="w-full" />
          <Skeleton height="h-3" width="w-5/6" />
          <Skeleton height="h-3" width="w-2/3" />
        </div>
      ) : (
        <div ref={containerRef}>{children}</div>
      )}
    </div>
  );
}
