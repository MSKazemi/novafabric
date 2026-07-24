import type { ReactNode } from 'react';
import { useChartExport } from '../../lib/useChartExport';

/**
 * Card wrapper for dashboard SVG charts (matches the AnalyticsTab chart-card
 * markup) with a compact SVG / PNG image-export affordance (ADR-0201).
 * Exports resolve theme tokens to concrete colors at the active theme.
 */
export default function ChartCard({
  title,
  legend,
  filename,
  children,
}: {
  title: string;
  legend?: ReactNode;
  /** Download base name (no extension). Defaults to a slug of `title`. */
  filename?: string;
  children: ReactNode;
}) {
  const base = filename ?? title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const { containerRef, exportSvg, exportPng } = useChartExport(base);

  const exportBtnClass =
    'font-mono text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:underline underline-offset-2';

  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          {title}
        </span>
        <span className="flex items-center gap-3">
          {legend}
          <span className="flex items-center gap-1 text-[10px] font-mono text-[var(--color-text-faint)]">
            <span aria-hidden="true">⬇</span>
            <button type="button" onClick={exportSvg} title={`Download “${title}” as SVG`} className={exportBtnClass}>
              SVG
            </button>
            <span aria-hidden="true">/</span>
            <button type="button" onClick={exportPng} title={`Download “${title}” as PNG`} className={exportBtnClass}>
              PNG
            </button>
          </span>
        </span>
      </div>
      <div ref={containerRef}>{children}</div>
    </div>
  );
}
