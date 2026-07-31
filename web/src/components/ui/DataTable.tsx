/**
 * Dense, virtualized data table shared across tabs.
 *
 * Virtual scrolling (TanStack Virtual) keeps thousands of rows smooth; built-in
 * sort, sticky header, row-click, a row-actions slot, and integrated
 * loading/empty/error states mean new tabs get a consistent, performant table
 * without re-implementing it each time.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { clsx } from 'clsx';
import { SkeletonRows } from './Skeleton';
import EmptyState from './EmptyState';
import { ErrorBox } from '../dashboard/helpers';

export interface Column<T> {
  key: string;
  header: ReactNode;
  /** Cell renderer; defaults to String(row[key]). */
  render?: (row: T, index: number) => ReactNode;
  /** Value extractor for sorting; enables the sort toggle on this column. */
  sortValue?: (row: T) => string | number;
  /** Tailwind width/grow class, e.g. "w-32" or "flex-1". */
  className?: string;
  align?: 'left' | 'right' | 'center';
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
  /** Per-row trailing controls. */
  rowActions?: (row: T) => ReactNode;
  /** Fixed row height in px (virtualization estimate). */
  rowHeight?: number;
  /** Max viewport height for the scroll area. */
  maxHeight?: number;
  /**
   * Infinite scroll: called when the user scrolls near the last row.
   * Pair with usePaginatedQuery — pass its loadMore when hasMore && !loadingMore.
   */
  onEndReached?: () => void;
  /** Footer slot below the scroll area (TruncationNotice, summaries). */
  footer?: ReactNode;
  className?: string;
}

const ALIGN: Record<NonNullable<Column<unknown>['align']>, string> = {
  left: 'text-left justify-start',
  right: 'text-right justify-end',
  center: 'text-center justify-center',
};

export default function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  error = null,
  onRetry,
  empty,
  onRowClick,
  rowActions,
  rowHeight = 40,
  maxHeight = 560,
  onEndReached,
  footer,
  className = '',
}: DataTableProps<T>) {
  const parentRef = useRef<HTMLDivElement>(null);
  const [sort, setSort] = useState<{ key: string; dir: 'asc' | 'desc' } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const get = col.sortValue;
    const dir = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = get(a);
      const vb = get(b);
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return 0;
    });
  }, [rows, sort, columns]);

  const virt = useVirtualizer({
    count: sorted.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => rowHeight,
    overscan: 12,
  });

  const toggleSort = (key: string) => {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: 'asc' };
      if (prev.dir === 'asc') return { key, dir: 'desc' };
      return null;
    });
  };

  // Infinite scroll: fire onEndReached when the last virtual row comes into
  // view. The virtualizer's item list is the cheapest reliable signal.
  const virtualItems = virt.getVirtualItems();
  const lastVisibleIndex = virtualItems.length > 0 ? virtualItems[virtualItems.length - 1].index : -1;
  const endReachedRef = useRef(onEndReached);
  endReachedRef.current = onEndReached;
  useEffect(() => {
    if (!endReachedRef.current) return;
    if (sorted.length > 0 && lastVisibleIndex >= sorted.length - 4) {
      endReachedRef.current();
    }
  }, [lastVisibleIndex, sorted.length]);

  if (error) return <ErrorBox message={error} onRetry={onRetry} />;
  if (loading) return <SkeletonRows rows={8} />;
  if (sorted.length === 0) {
    return <>{empty ?? <EmptyState message="No data." />}</>;
  }

  return (
    <div className={clsx('rounded border border-[var(--color-border)] overflow-hidden', className)}>
      {/* Header */}
      <div className="flex items-center gap-3 px-3 py-2 bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)] text-[var(--text-2xs)] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        {columns.map((col) => (
          <div
            key={col.key}
            className={clsx('flex items-center gap-1', col.className ?? 'flex-1', ALIGN[col.align ?? 'left'])}
          >
            {col.sortValue ? (
              <button
                type="button"
                onClick={() => toggleSort(col.key)}
                className="inline-flex items-center gap-1 hover:text-[var(--color-text)] transition-colors uppercase"
              >
                {col.header}
                <span aria-hidden="true" className="text-[var(--text-2xs)]">
                  {sort?.key === col.key ? (sort.dir === 'asc' ? '▲' : '▼') : '↕'}
                </span>
              </button>
            ) : (
              col.header
            )}
          </div>
        ))}
        {rowActions && <div className="w-px" aria-hidden="true" />}
      </div>

      {/* Virtualized body */}
      <div ref={parentRef} className="overflow-y-auto" style={{ maxHeight }}>
        <div style={{ height: virt.getTotalSize(), position: 'relative' }}>
          {virt.getVirtualItems().map((vi) => {
            const row = sorted[vi.index];
            return (
              <div
                key={rowKey(row, vi.index)}
                className={clsx(
                  'absolute left-0 right-0 flex items-center gap-3 px-3 border-b border-[var(--color-border)] text-xs text-[var(--color-text)]',
                  onRowClick && 'cursor-pointer hover:bg-[var(--color-bg-raised)] transition-colors',
                )}
                style={{ top: 0, transform: `translateY(${vi.start}px)`, height: vi.size }}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
                {columns.map((col) => (
                  <div
                    key={col.key}
                    className={clsx('truncate flex items-center', col.className ?? 'flex-1', ALIGN[col.align ?? 'left'])}
                  >
                    {col.render ? col.render(row, vi.index) : String((row as Record<string, unknown>)[col.key] ?? '')}
                  </div>
                ))}
                {rowActions && (
                  <div
                    className="flex items-center gap-1 shrink-0"
                    onClick={(e) => e.stopPropagation()}
                  >
                    {rowActions(row)}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {footer}
    </div>
  );
}
