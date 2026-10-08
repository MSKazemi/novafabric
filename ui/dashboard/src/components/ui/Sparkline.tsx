/**
 * Zero-dependency inline-SVG sparkline. Used for KPI trends on the Home tab and
 * eval-score history. Avoids pulling in a charting library (ADR-0024 Tier-A
 * dependency minimalism).
 *
 * v2: gradient area fill, min/max/last markers, and a hover crosshair with a
 * value readout — still pure SVG.
 */
import { useId, useState } from 'react';
import { clsx } from 'clsx';
import { formatSI } from '../../lib/chartFormat';

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  /** CSS color (var or literal). Defaults to the accent color. */
  stroke?: string;
  /** Fill area under the line with a faint gradient. */
  fill?: boolean;
  /** Dots on the min/max/last points. */
  markers?: boolean;
  /** Hover crosshair + value readout. */
  interactive?: boolean;
  className?: string;
  title?: string;
}

export default function Sparkline({
  values,
  width = 96,
  height = 24,
  stroke = 'var(--color-accent)',
  fill = true,
  markers = false,
  interactive = false,
  className = '',
  title,
}: SparklineProps) {
  const gradientId = useId();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (values.length < 2) {
    return <span className={clsx('text-2xs text-[var(--color-text-faint)]', className)}>—</span>;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const stepX = width / (values.length - 1);

  const points = values.map((v, i) => {
    const x = i * stepX;
    const y = height - ((v - min) / span) * (height - 4) - 2;
    return [x, y] as const;
  });

  const linePath = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const areaPath = `${linePath} L${width},${height} L0,${height} Z`;

  const minIdx = values.indexOf(min);
  const maxIdx = values.indexOf(max);
  const lastIdx = values.length - 1;

  const onMove = interactive
    ? (e: React.PointerEvent<SVGSVGElement>) => {
        const rect = e.currentTarget.getBoundingClientRect();
        const x = ((e.clientX - rect.left) / rect.width) * width;
        setHoverIdx(Math.max(0, Math.min(values.length - 1, Math.round(x / stepX))));
      }
    : undefined;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      role="img"
      aria-label={title ?? 'trend'}
      onPointerMove={onMove}
      onPointerLeave={interactive ? () => setHoverIdx(null) : undefined}
    >
      {title && <title>{title}</title>}
      {fill && (
        <>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <path d={areaPath} fill={`url(#${gradientId})`} />
        </>
      )}
      <path d={linePath} fill="none" stroke={stroke} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      {markers && (
        <>
          {maxIdx !== lastIdx && <circle cx={points[maxIdx][0]} cy={points[maxIdx][1]} r={1.75} fill={stroke} />}
          {minIdx !== lastIdx && minIdx !== maxIdx && (
            <circle cx={points[minIdx][0]} cy={points[minIdx][1]} r={1.75} fill={stroke} opacity={0.5} />
          )}
          <circle cx={points[lastIdx][0]} cy={points[lastIdx][1]} r={2} fill={stroke} />
        </>
      )}
      {interactive && hoverIdx !== null && (
        <>
          <line
            x1={points[hoverIdx][0]}
            x2={points[hoverIdx][0]}
            y1={0}
            y2={height}
            stroke="var(--color-border-strong)"
            strokeWidth={1}
          />
          <circle cx={points[hoverIdx][0]} cy={points[hoverIdx][1]} r={2.25} fill={stroke} />
          <text
            x={points[hoverIdx][0] + (hoverIdx > values.length / 2 ? -4 : 4)}
            y={9}
            textAnchor={hoverIdx > values.length / 2 ? 'end' : 'start'}
            className="fill-[var(--color-text)]"
            style={{ fontSize: 8, fontFamily: 'var(--font-mono)' }}
          >
            {formatSI(values[hoverIdx])}
          </text>
        </>
      )}
    </svg>
  );
}
