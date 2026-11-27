/**
 * One pagination hook for the dashboard's two server models:
 *
 *   cursor — ADR-0199 keyset endpoints: fetch(cursor?) → { items, next_cursor }
 *   offset — limit/offset endpoints:    fetch(offset, limit) → { items, total? }
 *
 * Exposes accumulated items + loadMore/hasMore plus the server's honesty
 * signals (`total` / `truncated` / `approximate`) so every list can render the
 * ADR-0199 "N more" affordance instead of silently cutting off.
 *
 * The accumulated list is bounded (`maxItems`, default `PAGINATED_MAX_ITEMS`):
 * once it is full, `capped` is true, `hasMore` is false and `loadMore` does
 * nothing, so an endless scroll cannot grow memory without limit. A page that
 * does not fit is cut, and because no further page is then fetched the server
 * cursor is never reused past the cut. `reset()` (or a deps change) starts over.
 */
import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';
import { PAGINATED_MAX_ITEMS, appendBounded } from './boundedList';

export interface CursorPage<T> {
  items: T[];
  next_cursor?: string | null;
  /** Server-reported total (may be approximate). */
  total?: number | null;
  approximate?: boolean;
}

export interface OffsetPage<T> {
  items: T[];
  total?: number | null;
  has_more?: boolean;
}

export type PaginatedSource<T> =
  | { mode: 'cursor'; fetch: (cursor: string | null) => Promise<CursorPage<T>> }
  | { mode: 'offset'; pageSize: number; fetch: (offset: number, limit: number) => Promise<OffsetPage<T>> };

export interface PaginatedState<T> {
  items: T[];
  loading: boolean;
  /** True only while fetching an ADDITIONAL page (initial load uses `loading`). */
  loadingMore: boolean;
  error: string | null;
  /** Total items server-side, when reported. */
  total: number | null;
  approximate: boolean;
  hasMore: boolean;
  /** The list reached `maxItems`; no more pages are loaded (UI: "showing the first N"). */
  capped: boolean;
  /** The in-memory maximum in effect. */
  maxItems: number;
  loadMore: () => void;
  /** Drop everything and refetch the first page. */
  reset: () => void;
}

/**
 * The list is "capped" when a page had to be cut, or when it is exactly full
 * while the server still has more. A list that simply ends at the limit is not.
 */
export function reachesCap(held: number, incoming: number, serverHasMore: boolean, max: number): boolean {
  const n = held + incoming;
  return n > max || (n >= max && serverHasMore);
}

export function usePaginatedQuery<T>(
  source: PaginatedSource<T>,
  deps: DependencyList = [],
  opts: { maxItems?: number } = {},
): PaginatedState<T> {
  const maxItems = Math.max(1, Math.floor(opts.maxItems ?? PAGINATED_MAX_ITEMS));
  const [items, setItems] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [approximate, setApproximate] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [resetTick, setResetTick] = useState(0);
  const [capped, setCapped] = useState(false);

  // Pagination position — refs so loadMore stays stable.
  const cursorRef = useRef<string | null>(null);
  const offsetRef = useRef(0);
  const inflight = useRef(0);
  const sourceRef = useRef(source);
  sourceRef.current = source;
  const countRef = useRef(0);
  const maxRef = useRef(maxItems);
  maxRef.current = maxItems;

  // Replace or append, never past the cap.
  const store = (prev: T[], pageItems: T[], append: boolean): T[] => {
    const next = appendBounded(append ? prev : [], pageItems, maxRef.current).items;
    countRef.current = next.length;
    return next;
  };

  const fetchPage = useCallback(async (append: boolean) => {
    if (append && countRef.current >= maxRef.current) return;
    const ticket = ++inflight.current;
    if (append) setLoadingMore(true);
    else { setLoading(true); setError(null); }
    try {
      const src = sourceRef.current;
      if (src.mode === 'cursor') {
        const page = await src.fetch(append ? cursorRef.current : null);
        if (ticket !== inflight.current) return;
        cursorRef.current = page.next_cursor ?? null;
        const more = Boolean(page.next_cursor);
        const full = reachesCap(append ? countRef.current : 0, page.items.length, more, maxRef.current);
        setItems((prev) => store(prev, page.items, append));
        setCapped(full);
        setHasMore(more && !full);
        if (page.total !== undefined) setTotal(page.total ?? null);
        setApproximate(Boolean(page.approximate));
      } else {
        const offset = append ? offsetRef.current : 0;
        const page = await src.fetch(offset, src.pageSize);
        if (ticket !== inflight.current) return;
        offsetRef.current = offset + page.items.length;
        const more = page.has_more ?? (page.total != null
          ? offset + page.items.length < page.total
          : page.items.length === src.pageSize);
        const full = reachesCap(append ? countRef.current : 0, page.items.length, more, maxRef.current);
        setItems((prev) => store(prev, page.items, append));
        if (page.total !== undefined) setTotal(page.total ?? null);
        setCapped(full);
        setHasMore(more && !full);
      }
    } catch (e) {
      if (ticket !== inflight.current) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (ticket === inflight.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  useEffect(() => {
    cursorRef.current = null;
    offsetRef.current = 0;
    void fetchPage(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, resetTick]);

  const loadMore = useCallback(() => {
    void fetchPage(true);
  }, [fetchPage]);

  const reset = useCallback(() => setResetTick((t) => t + 1), []);

  return { items, loading, loadingMore, error, total, approximate, hasMore, capped, maxItems, loadMore, reset };
}
