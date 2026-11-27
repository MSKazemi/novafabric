/**
 * Data layer for the Runs tab: cursor-paginated search (B-1), SSE live-stream
 * prepend (B-3), and the per-run ClickHouse cost-summary fetch.
 *
 * Extracted from the former RunsTab monolith. The list is bounded to
 * `LIVE_RUNS_MAX` runs and carries a freshness tracker (issue #282). The
 * SSE stream mutates the accumulated list in place (prepend) and deletes
 * filter it, which is why this stays a bespoke hook rather than adopting
 * `usePaginatedQuery` (the shared hook owns its items internally and has no
 * external mutation path).
 */
import { useState, useEffect, useCallback, useRef, type Dispatch, type SetStateAction } from 'react';
import { api, getConnection, openManagedRunStream, type RunStreamHandle } from '../../../../lib/api';
import type { RunSummary } from '../../../../lib/api';
import type { RunCostEntry, StatusFilter } from './types';
import { LIVE_RUNS_MAX, appendBounded, chunk, isAtCap, prependBounded, pruneRecord } from '../../../../lib/boundedList';
import type { LiveTracker } from '../../../../lib/freshness';
import { useLiveTracker } from '../../../../lib/useLiveTracker';

const PAGE_SIZE = 50;

/** `/api/runs/cost-summary` answers for at most 100 run ids per request. */
export const COST_SUMMARY_BATCH = 100;

/**
 * Run ids whose cost still needs fetching: those not asked about yet, plus
 * running runs (their cost is still growing). Finished runs already asked
 * about are not asked again, so a pushed run costs one small request, not a
 * re-fetch of every listed id.
 */
export function costIdsToFetch(
  runs: readonly Pick<RunSummary, 'run_id' | 'status'>[],
  asked: ReadonlySet<string>,
): string[] {
  return runs
    .filter((r) => !asked.has(r.run_id) || r.status === 'running')
    .map((r) => r.run_id);
}

/**
 * The server keeps rows whose `created_at <= until` as a string comparison, so a bare
 * `YYYY-MM-DD` excluded the whole selected day (the client-side filter used end of day).
 * Review frontend-dashboard-19.
 */
export function endOfDay(until: string): string | undefined {
  if (!until) return undefined;
  return /^\d{4}-\d{2}-\d{2}$/.test(until) ? `${until}T23:59:59.999999` : until;
}

export interface RunSearchState {
  runs: RunSummary[] | null;
  setRuns: Dispatch<SetStateAction<RunSummary[] | null>>;
  error: string | null;
  totalApprox: number;
  hasMore: boolean;
  loadingMore: boolean;
  liveConnected: boolean;
  /** How current the list is (stream + reload state), for `FreshnessIndicator`. */
  liveTracker: LiveTracker;
  /** The list holds `listMax` runs: no further page is loaded, the oldest are dropped. */
  capped: boolean;
  /** The named in-memory maximum for this list (`LIVE_RUNS_MAX`). */
  listMax: number;
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
  selectedRunId = null,
}: {
  search: string;
  statusFilter: StatusFilter;
  since: string;
  until: string;
  refreshTick: number;
  onCountChange?: (n: number) => void;
  /** The selected run is never the one dropped when the list is at its cap. */
  selectedRunId?: string | null;
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
  const [liveTracker, dispatchLive] = useLiveTracker();
  const selectedRef = useRef(selectedRunId);
  useEffect(() => { selectedRef.current = selectedRunId; }, [selectedRunId]);
  // Run ids the cost endpoint has answered for (bounded: pruned to the list).
  const costAskedRef = useRef<Set<string>>(new Set());
  const capped = isAtCap(runs, LIVE_RUNS_MAX);

  // Each refresh starts a new "generation"; a response from an older one is dropped, so
  // typing "ab" cannot end with the results for "a", and a loadMore begun before a filter
  // change cannot append the old filter's page to the new filter's list (review
  // frontend-dashboard-12).
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++generation.current;
    setError(null);
    setNextCursor(null);
    try {
      const r = await api.searchRuns({
        limit: PAGE_SIZE,
        q: search.trim() || undefined,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        since: since || undefined,
        until: endOfDay(until),
      });
      if (mine !== generation.current) return;
      setRuns(r.items);
      setNextCursor(r.next_cursor);
      setTotalApprox(r.total_approx);
      totalApproxRef.current = r.total_approx;
      setHasMore(r.next_cursor !== null);
      onCountChange?.(r.total_approx);
      dispatchLive({ type: 'synced', at: Date.now() });
    } catch (e) {
      if (mine === generation.current) {
        setError((e as Error).message);
        dispatchLive({ type: 'sync-failed', at: Date.now() });
      }
    }
  }, [onCountChange, search, since, until, statusFilter, dispatchLive]);
  const refreshRef = useRef(refresh);
  useEffect(() => { refreshRef.current = refresh; }, [refresh]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore || capped) return;
    const mine = generation.current;
    setLoadingMore(true);
    try {
      const r = await api.searchRuns({
        limit: PAGE_SIZE,
        cursor: nextCursor,
        q: search.trim() || undefined,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        since: since || undefined,
        until: endOfDay(until),
      });
      if (mine !== generation.current) return; // the list was refreshed meanwhile
      // Bounded (LIVE_RUNS_MAX): a page that does not fit loses its oldest rows,
      // and `capped` then stops further loads, so the stale cursor is never used.
      setRuns(prev => appendBounded(prev, r.items, LIVE_RUNS_MAX).items);
      setNextCursor(r.next_cursor);
      setHasMore(r.next_cursor !== null);
    } catch (e) {
      if (mine === generation.current) setError((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  }, [nextCursor, loadingMore, capped, search, since, until, statusFilter]);

  useEffect(() => { refresh(); }, [refresh, refreshTick]);
  // Keep ref current so the SSE callback (captured once) always calls the latest prop
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  // SSE live stream — prepend newly-captured runs (B-3), bounded to LIVE_RUNS_MAX
  // (the oldest drop off the tail). The managed stream reconnects with jittered
  // backoff; an open after a gap reloads the list, because the stream does not
  // replay the runs it missed, and the freshness tracker says "refreshing…" until then.
  useEffect(() => {
    const handle = openManagedRunStream(
      (newRun) => {
        setRuns(prev => prependBounded(prev, newRun, LIVE_RUNS_MAX, r => r.run_id, selectedRef.current));
        dispatchLive({ type: 'stream-message', at: Date.now() });
        const next = totalApproxRef.current + 1;
        totalApproxRef.current = next;
        setTotalApprox(next);
        onCountChangeRef.current?.(next);
      },
      (connected, status) => {
        setLiveConnected(connected);
        const at = Date.now();
        if (connected) {
          const reconnected = Boolean(status?.reconnected);
          dispatchLive({ type: 'stream-open', at, reconnected });
          if (reconnected) void refreshRef.current();
        } else {
          dispatchLive({ type: 'stream-down', at });
        }
      },
    );
    sseRef.current = handle;
    return () => { handle.close(); setLiveConnected(false); };
  // only open once; don't re-open on filter changes
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch per-run cost summary whenever the run list changes: only ids not yet
  // asked about (plus running runs), in batches the server accepts (100 ids each).
  // Degrades silently when ClickHouse is absent (endpoint returns {costs:{}}).
  useEffect(() => {
    if (!runs || runs.length === 0) return;
    const { token, base } = getConnection();
    if (!token) return;
    const keep = runs.map(r => r.run_id);
    const listed = new Set(keep);
    for (const id of costAskedRef.current) if (!listed.has(id)) costAskedRef.current.delete(id);
    const wanted = costIdsToFetch(runs, costAskedRef.current);
    let cancelled = false;
    for (const batch of chunk(wanted, COST_SUMMARY_BATCH)) {
      const url = `${base}/api/runs/cost-summary?token=${encodeURIComponent(token)}&run_ids=${encodeURIComponent(batch.join(','))}`;
      fetch(url)
        .then(res => res.ok ? res.json() : null)
        .then((data: { costs?: Record<string, RunCostEntry> } | null) => {
          if (cancelled || data === null) return; // a superseded effect re-asks these ids
          for (const id of batch) costAskedRef.current.add(id);
          if (data.costs && Object.keys(data.costs).length > 0) {
            // Keep cost entries only for runs still in the (bounded) list.
            setCostMap(prev => pruneRecord({ ...prev, ...data.costs }, keep));
          }
        })
        .catch(() => { /* ClickHouse absent — degrade silently */ });
    }
    return () => { cancelled = true; };
  }, [runs]);

  return {
    runs, setRuns, error, totalApprox,
    // At the cap there is no further page to load: the UI says "showing the latest N".
    hasMore: hasMore && !capped,
    loadingMore, liveConnected, liveTracker, capped, listMax: LIVE_RUNS_MAX,
    costMap, refresh, loadMore,
  };
}
