/**
 * Issue #282 — the Runs list fed by the SSE stream stays bounded, stops paging
 * at its cap, and reloads after a reconnect before it is called live again.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RunStreamStatus, RunSummary } from '@/lib/api';

const searchRuns = vi.fn();
let pushRun: (r: RunSummary) => void = () => {};
let pushStatus: (connected: boolean, s: RunStreamStatus) => void = () => {};

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  api: { searchRuns: (...a: unknown[]) => searchRuns(...a) },
  getConnection: () => ({ token: null, base: 'http://127.0.0.1:4321' }),
  openManagedRunStream: (
    onRun: (r: RunSummary) => void,
    onStatus?: (connected: boolean, s: RunStreamStatus) => void,
  ) => {
    pushRun = onRun;
    pushStatus = onStatus ?? (() => {});
    return { close: () => {} };
  },
}));

import { useRunSearch } from '@/components/dashboard/tabs/runs/useRunSearch';
import { LIVE_RUNS_MAX } from '@/lib/boundedList';
import { livePhase } from '@/lib/freshness';

const page = (ids: string[], cursor: string | null = null, total = ids.length) => ({
  items: ids.map((run_id) => ({ run_id })),
  next_cursor: cursor,
  total_approx: total,
});
const ids = (n: number, p: string) => Array.from({ length: n }, (_, i) => `${p}${i}`);
const props = { search: '', statusFilter: 'all' as const, since: '', until: '', refreshTick: 0 };
const status = (connected: boolean, reconnected = false): RunStreamStatus => ({
  connected, reconnected, attempt: 0, retryInMs: null, waitingForNetwork: false,
});

describe('useRunSearch live list', () => {
  beforeEach(() => searchRuns.mockReset());

  it('keeps at most LIVE_RUNS_MAX runs, dropping the oldest', async () => {
    searchRuns.mockResolvedValueOnce(page(ids(50, 'old'), 'c1', 10_000));
    const { result } = renderHook(() => useRunSearch(props));
    await waitFor(() => expect(result.current.runs).toHaveLength(50));

    act(() => {
      for (let i = 0; i < LIVE_RUNS_MAX + 20; i++) pushRun({ run_id: `new${i}` } as RunSummary);
    });
    const runs = result.current.runs!;
    expect(runs).toHaveLength(LIVE_RUNS_MAX);
    expect(runs[0].run_id).toBe(`new${LIVE_RUNS_MAX + 19}`);
    expect(runs.some((r) => r.run_id.startsWith('old'))).toBe(false);
    expect(result.current.capped).toBe(true);
    expect(result.current.listMax).toBe(LIVE_RUNS_MAX);
    expect(result.current.hasMore).toBe(false); // no paging past the cap
  });

  it('does not fetch another page once the cap is reached', async () => {
    searchRuns.mockResolvedValueOnce(page(ids(LIVE_RUNS_MAX, 'r'), 'c1', 10_000));
    const { result } = renderHook(() => useRunSearch(props));
    await waitFor(() => expect(result.current.capped).toBe(true));
    await act(async () => { await result.current.loadMore(); });
    expect(searchRuns).toHaveBeenCalledTimes(1);
  });

  it('cuts a loaded page that would overflow the cap', async () => {
    searchRuns
      .mockResolvedValueOnce(page(ids(LIVE_RUNS_MAX - 10, 'a'), 'c1', 10_000))
      .mockResolvedValueOnce(page(ids(50, 'b'), 'c2', 10_000));
    const { result } = renderHook(() => useRunSearch(props));
    await waitFor(() => expect(result.current.runs).toHaveLength(LIVE_RUNS_MAX - 10));
    await act(async () => { await result.current.loadMore(); });
    expect(result.current.runs).toHaveLength(LIVE_RUNS_MAX);
    expect(result.current.capped).toBe(true);
  });

  it('reloads after a reconnect and is not live until the reload lands', async () => {
    searchRuns.mockResolvedValueOnce(page(['r1']));
    const { result } = renderHook(() => useRunSearch(props));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    act(() => pushStatus(true, status(true)));
    expect(livePhase(result.current.liveTracker, Date.now())).toBe('live');

    act(() => pushStatus(false, status(false)));
    expect(livePhase(result.current.liveTracker, Date.now())).toBe('reconnecting');

    let resolveResync!: (v: ReturnType<typeof page>) => void;
    searchRuns.mockReturnValueOnce(new Promise((res) => { resolveResync = res; }));
    act(() => pushStatus(true, status(true, true)));
    expect(searchRuns).toHaveBeenCalledTimes(2);
    expect(livePhase(result.current.liveTracker, Date.now())).toBe('resyncing');

    await act(async () => { resolveResync(page(['r2', 'r1'])); });
    await waitFor(() => expect(result.current.runs?.map((r) => r.run_id)).toEqual(['r2', 'r1']));
    expect(livePhase(result.current.liveTracker, Date.now())).toBe('live');
  });

  it('a failed reload leaves the list labelled stale, never live', async () => {
    searchRuns.mockResolvedValueOnce(page(['r1']));
    const { result } = renderHook(() => useRunSearch(props));
    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    searchRuns.mockRejectedValueOnce(new Error('server unreachable'));
    await act(async () => { pushStatus(true, status(true, true)); });
    await waitFor(() => expect(livePhase(result.current.liveTracker, Date.now())).toBe('stale'));
    expect(result.current.runs).toHaveLength(1); // the old data stays visible, labelled
  });
});
