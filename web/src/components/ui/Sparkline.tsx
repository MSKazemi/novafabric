/**
 * Zero-dependency inline-SVG sparkline. Used for KPI trends on the Home tab and
 * eval-score history. Avoids pulling in a charting library (ADR-0024 Tier-A
 * dependency minimalism).
 */
import { clsx } from 'clsx';

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  /** CSS color (var or literal). Defaults to the accent color. */
  stroke?: string;
  /** Fill area under the line with a faint tint. */
  fill?: boolean;
  className?: string;
  title?: string;
}

export default function Sparkline({
  values,
  width = 96,
  height = 24,
  stroke = 'var(--color-accent)',
  fill = true,
  className = '',
  title,
}: SparklineProps) {
  if (values.length < 2) {
    return <span className={clsx('text-[10px] text-[var(--color-text-faint)]', className)}>—</span>;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const stepX = width / (values.length - 1);

  const points = values.map((v, i) => {
    const x = i * stepX;
    const y = height - ((v - min) / span) * (height - 2) - 1;
    return [x, y] as const;
  });

  const linePath = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const areaPath = `${linePath} L${width},${height} L0,${height} Z`;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      role="img"
      aria-label={title ?? 'trend'}
    >
      {title && <title>{title}</title>}
      {fill && <path d={areaPath} fill={stroke} opacity={0.12} />}
      <path d={linePath} fill="none" stroke={stroke} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
