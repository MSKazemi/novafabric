/**
 * Issue #282 — every in-memory list fed by the live stream or by repeated
 * paging has a named maximum; the oldest entries drop, and the cut is visible.
 */
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  LIVE_RUNS_MAX,
  PAGINATED_MAX_ITEMS,
  appendBounded,
  isAtCap,
  prependBounded,
  pruneRecord,
} from '@/lib/boundedList';
import { reachesCap, usePaginatedQuery } from '@/lib/usePaginatedQuery';
import TruncationNotice from '@/components/ui/TruncationNotice';

const id = (x: { id: string }) => x.id;
const rows = (n: number, prefix = 'r') => Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }));

describe('named maxima', () => {
  it('are positive and the live list is the smaller one', () => {
    expect(LIVE_RUNS_MAX).toBeGreaterThan(0);
    expect(PAGINATED_MAX_ITEMS).toBeGreaterThanOrEqual(LIVE_RUNS_MAX);
  });
});

describe('prependBounded', () => {
  it('puts the new item first', () => {
    expect(prependBounded([{ id: 'a' }], { id: 'b' }, 10, id).map(id)).toEqual(['b', 'a']);
  });

  it('starts a list from null', () => {
    expect(prependBounded(null, { id: 'a' }, 10, id)).toEqual([{ id: 'a' }]);
  });

  it('returns the same list for a duplicate key', () => {
    const list = [{ id: 'a' }, { id: 'b' }];
    expect(prependBounded(list, { id: 'b' }, 10, id)).toBe(list);
  });

  it('drops the oldest (tail) entries beyond the maximum', () => {
    let list: { id: string }[] = [];
    for (let i = 0; i < 25; i++) list = prependBounded(list, { id: `n${i}` }, 10, id);
    expect(list).toHaveLength(10);
    expect(list[0].id).toBe('n24');
    expect(list[9].id).toBe('n15');
  });

  it('stays flat under a long stream (memory does not grow)', () => {
    let list: { id: string }[] = rows(LIVE_RUNS_MAX, 'old');
    for (let i = 0; i < 5 * LIVE_RUNS_MAX; i++) list = prependBounded(list, { id: `new${i}` }, LIVE_RUNS_MAX, id);
    expect(list).toHaveLength(LIVE_RUNS_MAX);
    expect(list.some((r) => r.id.startsWith('old'))).toBe(false);
  });

  it('clamps a nonsensical maximum to 1', () => {
    expect(prependBounded([{ id: 'a' }], { id: 'b' }, 0, id)).toEqual([{ id: 'b' }]);
    expect(prependBounded([{ id: 'a' }], { id: 'b' }, Number.NaN, id)).toEqual([{ id: 'b' }]);
  });
});

describe('appendBounded', () => {
  it('appends a page that fits', () => {
    expect(appendBounded(rows(2), rows(3, 'p'), 10)).toEqual({ items: [...rows(2), ...rows(3, 'p')], dropped: 0 });
  });

  it('cuts the oldest rows of a page that does not fit', () => {
    const { items, dropped } = appendBounded(rows(8), rows(5, 'p'), 10);
    expect(items).toHaveLength(10);
    expect(items.slice(8).map(id)).toEqual(['p0', 'p1']);
    expect(dropped).toBe(3);
  });

  it('appends nothing to a full list', () => {
    expect(appendBounded(rows(10), rows(5, 'p'), 10)).toEqual({ items: rows(10), dropped: 5 });
  });
});

describe('isAtCap / pruneRecord / reachesCap', () => {
  it('isAtCap', () => {
    expect(isAtCap(null, 3)).toBe(false);
    expect(isAtCap(rows(2), 3)).toBe(false);
    expect(isAtCap(rows(3), 3)).toBe(true);
  });

  it('pruneRecord keeps only the listed keys', () => {
    expect(pruneRecord({ a: 1, b: 2, c: 3 }, ['a', 'c', 'z'])).toEqual({ a: 1, c: 3 });
    expect(pruneRecord({ a: 1 }, [])).toEqual({});
  });

  it('reachesCap: cut, or exactly full with more on the server', () => {
    expect(reachesCap(8, 5, false, 10)).toBe(true);
    expect(reachesCap(5, 5, true, 10)).toBe(true);
    expect(reachesCap(5, 5, false, 10)).toBe(false); // the list simply ends at the limit
    expect(reachesCap(2, 3, true, 10)).toBe(false);
  });
});

describe('usePaginatedQuery maxItems', () => {
  it('stops loading at the cap and reports capped', async () => {
    let n = 0;
    const fetch = vi.fn(async () => {
      const page = rows(3, `c${n}-`);
      n += 1;
      return { items: page, next_cursor: `cur${n}` };
    });
    const { result } = renderHook(() =>
      usePaginatedQuery({ mode: 'cursor', fetch }, [], { maxItems: 7 }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.items).toHaveLength(3);
    expect(result.current.capped).toBe(false);

    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(6));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(7));
    expect(result.current.capped).toBe(true);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.maxItems).toBe(7);

    const calls = fetch.mock.calls.length;
    act(() => result.current.loadMore());
    await new Promise((r) => setTimeout(r, 0));
    expect(fetch.mock.calls.length).toBe(calls); // no request past the cap
  });

  it('reset() starts over below the cap', async () => {
    const fetch = vi.fn(async () => ({ items: rows(5), next_cursor: 'more' }));
    const { result } = renderHook(() =>
      usePaginatedQuery({ mode: 'cursor', fetch }, [], { maxItems: 5 }),
    );
    await waitFor(() => expect(result.current.capped).toBe(true));
    act(() => result.current.reset());
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(result.current.items).toHaveLength(5);
  });

  it('offset mode is bounded too', async () => {
    const fetch = vi.fn(async (offset: number, limit: number) => ({
      items: rows(limit, `o${offset}-`),
      total: 100,
    }));
    const { result } = renderHook(() =>
      usePaginatedQuery({ mode: 'offset', pageSize: 4, fetch }, [], { maxItems: 6 }),
    );
    await waitFor(() => expect(result.current.items).toHaveLength(4));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(6));
    expect(result.current.capped).toBe(true);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.total).toBe(100);
  });
});

describe('TruncationNotice capped', () => {
  it('says the display limit was reached even without a server total', () => {
    render(<TruncationNotice shown={2000} hasMore={false} capped />);
    expect(screen.getByText(/display limit reached/)).toBeInTheDocument();
  });

  it('renders nothing for a complete, uncapped list', () => {
    const { container } = render(<TruncationNotice shown={3} total={3} hasMore={false} />);
    expect(container).toBeEmptyDOMElement();
  });
});
