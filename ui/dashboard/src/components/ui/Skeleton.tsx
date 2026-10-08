/**
 * Animated placeholder blocks shown while data loads. Conveys the expected
 * layout (rows/cards) instead of a bare "Loading…", reducing perceived latency.
 */

interface SkeletonProps {
  /** Tailwind width class, e.g. "w-32" or "w-full". */
  width?: string;
  /** Tailwind height class, e.g. "h-4". */
  height?: string;
  className?: string;
}

export function Skeleton({ width = 'w-full', height = 'h-4', className = '' }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={`animate-pulse rounded bg-[color-mix(in_oklab,var(--color-text)_8%,transparent)] ${width} ${height} ${className}`}
    />
  );
}

/** A list of skeleton rows — a reasonable default placeholder for tables/lists. */
export function SkeletonRows({ rows = 6 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading" className="space-y-2 p-2">
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          className="flex items-center gap-3 rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-3 py-2.5"
        >
          <Skeleton width="w-2" height="h-2" className="rounded-full shrink-0" />
          <Skeleton width="w-40" height="h-3.5" />
          <div className="flex-1" />
          <Skeleton width="w-16" height="h-3" />
        </div>
      ))}
    </div>
  );
}

export default Skeleton;
