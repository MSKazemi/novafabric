/**
 * ADR-0173 trust-radar glyph — a zero-dependency inline-SVG polar chart of a
 * capsule's trust guarantees.
 *
 * Honesty rules the visual MUST preserve (ADR-0173):
 *  - an `na` axis is drawn as a hollow tick at the centre, never as a zero-
 *    length "fail" spoke: a guarantee the capsule cannot evidence is
 *    *unverified*, not *failed*;
 *  - `na` axes are excluded from the filled polygon entirely, so the shaded
 *    area only ever claims what was actually verified.
 */
import { useId } from 'react';
import { clsx } from 'clsx';
import type { RadarAxis, RadarAxisState } from '../../lib/api';

const STATE_COLOR: Record<RadarAxisState, string> = {
  ok: 'var(--color-status-success)',
  warn: 'var(--color-status-pending)',
  fail: 'var(--color-status-failure)',
  na: 'var(--color-text-faint)',
};

export interface TrustRadarGlyphProps {
  axes: RadarAxis[];
  size?: number;
  /** Show axis labels around the perimeter. */
  labels?: boolean;
  className?: string;
}

export default function TrustRadarGlyph({
  axes,
  size = 240,
  labels = true,
  className,
}: TrustRadarGlyphProps) {
  const gradientId = useId();
  if (axes.length < 3) {
    return (
      <p className="text-[var(--text-2xs)] text-[var(--color-text-faint)]">
        Not enough axes to plot ({axes.length}).
      </p>
    );
  }

  const pad = labels ? 46 : 12;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - pad;
  const n = axes.length;

  // -90° puts the first axis at 12 o'clock.
  const angleOf = (i: number) => (i / n) * 2 * Math.PI - Math.PI / 2;
  const pointAt = (i: number, reach: number) => {
    const a = angleOf(i);
    return [cx + Math.cos(a) * r * reach, cy + Math.sin(a) * r * reach] as const;
  };

  const rings = [0.25, 0.5, 0.75, 1];

  // The filled polygon covers only axes that are actually applicable.
  const applicable = axes
    .map((axis, i) => ({ axis, i }))
    .filter(({ axis }) => axis.state !== 'na' && axis.value !== null);
  const polygon =
    applicable.length >= 3
      ? applicable
          .map(({ axis, i }) => pointAt(i, axis.value ?? 0).map((v) => v.toFixed(1)).join(','))
          .join(' ')
      : null;

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className={clsx('overflow-visible', className)}
      role="img"
      aria-label="Trust radar"
    >
      <defs>
        <radialGradient id={gradientId}>
          <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.35} />
          <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0.12} />
        </radialGradient>
      </defs>

      {/* Web: concentric rings + spokes */}
      {rings.map((ring) => (
        <polygon
          key={ring}
          points={axes
            .map((_, i) => pointAt(i, ring).map((v) => v.toFixed(1)).join(','))
            .join(' ')}
          fill="none"
          stroke="var(--color-border)"
          strokeWidth={1}
        />
      ))}
      {axes.map((_, i) => {
        const [x, y] = pointAt(i, 1);
        return (
          <line
            key={i}
            x1={cx}
            y1={cy}
            x2={x}
            y2={y}
            stroke="var(--color-border)"
            strokeWidth={1}
          />
        );
      })}

      {/* Verified area — applicable axes only */}
      {polygon && (
        <polygon
          points={polygon}
          fill={`url(#${gradientId})`}
          stroke="var(--color-accent)"
          strokeWidth={1.5}
          strokeLinejoin="round"
        />
      )}

      {/* Per-axis markers */}
      {axes.map((axis, i) => {
        const isNa = axis.state === 'na' || axis.value === null;
        const [x, y] = pointAt(i, isNa ? 0.08 : (axis.value ?? 0));
        return (
          <circle
            key={axis.key}
            cx={x}
            cy={y}
            r={isNa ? 2.5 : 3.5}
            fill={isNa ? 'none' : STATE_COLOR[axis.state]}
            stroke={STATE_COLOR[axis.state]}
            strokeWidth={isNa ? 1.25 : 0}
            strokeDasharray={isNa ? '1.5 1.5' : undefined}
          >
            <title>
              {axis.label}: {isNa ? 'not applicable (unverified)' : `${Math.round((axis.value ?? 0) * 100)}% — ${axis.state}`}
            </title>
          </circle>
        );
      })}

      {/* Perimeter labels */}
      {labels &&
        axes.map((axis, i) => {
          const [x, y] = pointAt(i, 1.16);
          const a = angleOf(i);
          const anchor =
            Math.abs(Math.cos(a)) < 0.35 ? 'middle' : Math.cos(a) > 0 ? 'start' : 'end';
          return (
            <text
              key={axis.key}
              x={x}
              y={y + 3}
              textAnchor={anchor}
              className={clsx(
                axis.state === 'na'
                  ? 'fill-[var(--color-text-faint)]'
                  : 'fill-[var(--color-text-muted)]',
              )}
              style={{ fontSize: 9, fontFamily: 'var(--font-mono)' }}
            >
              {axis.label}
            </text>
          );
        })}
    </svg>
  );
}
