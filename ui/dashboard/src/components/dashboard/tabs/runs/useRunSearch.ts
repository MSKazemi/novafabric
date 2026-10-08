/**
 * Data layer for the Runs tab: cursor-paginated search (B-1), SSE live-stream
 * prepend (B-3), and the per-run ClickHouse cost-summary fetch.
 *
 * Extracted verbatim from the former RunsTab monolith — behavior frozen. The
 * SSE stream mutates the accumulated list in place (prepend) and deletes
 * filter it, which is why this stays a bespoke hook rather than adopting
 * `usePaginatedQuery` (the shared hook owns its items internally and has no
 * external mutation path).
 */
import { useState, useEffect, useCallback, useRef, type Dispatch, type SetStateAction } from 'react';
import { api, getConnection, openManagedRunStream, type RunStreamHandle } from '../../../../lib/api';
import type { RunSummary } from '../../../../lib/api';
import type { RunCostEntry, StatusFilter } from './types';

const PAGE_SIZE = 50;

export interface RunSearchState {
  runs: RunSummary[] | null;
  setRuns: Dispatch<SetStateAction<RunSummary[] | null>>;
  error: string | null;
  totalApprox: number;
  hasMore: boolean;
  loadingMore: boolean;
  liveConnected: boolean;
  costMap: Record<string, RunCostEntry>;
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
}

export function useRunSearch({
  search,
  statusFilter,
  since,
  until,
  refreshTick,
  onCountChange,
}: {
  search: string;
  statusFilter: StatusFilter;
  since: string;
  until: string;
  refreshTick: number;
  onCountChange?: (n: number) => void;
}): RunSearchState {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [costMap, setCostMap] = useState<Record<string, RunCostEntry>>({});

  // Cursor-based pagination state (B-1)
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [totalApprox, setTotalApprox] = useState(0);
  const totalApproxRef = useRef(0);
  const onCountChangeRef = useRef(onCountChange);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // SSE live indicator (B-3)
  const [liveConnected, setLiveConnected] = useState(false);
  const sseRef = useRef<RunStreamHandle | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    setNextCursor(null);
    try {
      const r = await api.searchRuns({
        limit: PAGE_SIZE,
        q: search.trim() || undefined,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        since: since || undefined,
        until: until || undefined,
      });
      setRuns(r.items);
      setNextCursor(r.next_cursor);
      setTotalApprox(r.total_approx);
      totalApproxRef.current = r.total_approx;
      setHasMore(r.next_cursor !== null);
      onCountChange?.(r.total_approx);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [onCountChange, search, since, until, statusFilter]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const r = await api.searchRuns({
        limit: PAGE_SIZE,
        cursor: nextCursor,
        q: search.trim() || undefined,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        since: since || undefined,
        until: until || undefined,
      });
      setRuns(prev => [...(prev ?? []), ...r.items]);
      setNextCursor(r.next_cursor);
      setHasMore(r.next_cursor !== null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  }, [nextCursor, loadingMore, search, since, until, statusFilter]);

  useEffect(() => { refresh(); }, [refresh, refreshTick]);
  // Keep ref current so the SSE callback (captured once) always calls the latest prop
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  // SSE live stream — prepend newly-captured runs (B-3).
  // Managed stream auto-reconnects with backoff and reports connection state.
  useEffect(() => {
    const handle = openManagedRunStream(
      (newRun) => {
        setRuns(prev => {
          if (!prev) return [newRun];
          if (prev.some(r => r.run_id === newRun.run_id)) return prev;
          return [newRun, ...prev];
        });
        const next = totalApproxRef.current + 1;
        totalApproxRef.current = next;
        setTotalApprox(next);
        onCountChangeRef.current?.(next);
      },
      (connected) => setLiveConnected(connected),
    );
    sseRef.current = handle;
    return () => { handle.close(); setLiveConnected(false); };
  // only open once; don't re-open on filter changes
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch per-run cost summary whenever the run list changes.
  // Degrades silently when ClickHouse is absent (endpoint returns {costs:{}}).
  useEffect(() => {
    if (!runs || runs.length === 0) return;
    const { token, base } = getConnection();
    if (!token) return;
    const ids = runs.map(r => r.run_id).join(',');
    const url = `${base}/api/runs/cost-summary?token=${encodeURIComponent(token)}&run_ids=${encodeURIComponent(ids)}`;
    let cancelled = false;
    fetch(url)
      .then(res => res.ok ? res.json() : Promise.resolve({ costs: {} }))
      .then((data: { costs?: Record<string, RunCostEntry> }) => {
        if (!cancelled && data.costs && Object.keys(data.costs).length > 0) {
          setCostMap(prev => ({ ...prev, ...data.costs }));
        }
      })
      .catch(() => { /* ClickHouse absent — degrade silently */ });
    return () => { cancelled = true; };
  }, [runs]);

  return { runs, setRuns, error, totalApprox, hasMore, loadingMore, liveConnected, costMap, refresh, loadMore };
}
