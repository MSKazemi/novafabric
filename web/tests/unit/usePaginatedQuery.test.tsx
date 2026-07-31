/**
 * usePaginatedQuery — both server pagination models + honesty signals.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { usePaginatedQuery } from '@/lib/usePaginatedQuery';
import { useQuery } from '@/lib/useQuery';
import TruncationNotice from '@/components/ui/TruncationNotice';

describe('usePaginatedQuery (cursor mode)', () => {
  it('accumulates pages until next_cursor is exhausted', async () => {
    const pages: Record<string, { items: number[]; next_cursor: string | null; total: number }> = {
      first: { items: [1, 2], next_cursor: 'c1', total: 5 },
      c1: { items: [3, 4], next_cursor: 'c2', total: 5 },
      c2: { items: [5], next_cursor: null, total: 5 },
    };
    const fetch = vi.fn(async (cursor: string | null) => pages[cursor ?? 'first']);
    const { result } = renderHook(() =>
      usePaginatedQuery({ mode: 'cursor', fetch }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.items).toEqual([1, 2]);
    expect(result.current.total).toBe(5);
    expect(result.current.hasMore).toBe(true);

    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toEqual([1, 2, 3, 4]));

    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toEqual([1, 2, 3, 4, 5]));
    expect(result.current.hasMore).toBe(false);
  });

  it('surfaces approximate totals', async () => {
    const { result } = renderHook(() =>
      usePaginatedQuery({
        mode: 'cursor',
        fetch: async () => ({ items: [1], next_cursor: null, total: 100, approximate: true }),
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.approximate).toBe(true);
    expect(result.current.total).toBe(100);
  });

  it('reports errors and reset refetches', async () => {
    let fail = true;
    const { result } = renderHook(() =>
      usePaginatedQuery({
        mode: 'cursor',
        fetch: async () => {
          if (fail) throw new Error('nope');
          return { items: ['ok'], next_cursor: null };
        },
      }),
    );
    await waitFor(() => expect(result.current.error).toBe('nope'));
    fail = false;
    act(() => result.current.reset());
    await waitFor(() => expect(result.current.items).toEqual(['ok']));
    expect(result.current.error).toBeNull();
  });
});

describe('usePaginatedQuery (offset mode)', () => {
  it('pages by offset and derives hasMore from total', async () => {
    const all = Array.from({ length: 5 }, (_, i) => i);
    const fetch = vi.fn(async (offset: number, limit: number) => ({
      items: all.slice(offset, offset + limit),
      total: all.length,
    }));
    const { result } = renderHook(() =>
      usePaginatedQuery({ mode: 'offset', pageSize: 2, fetch }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.items).toEqual([0, 1]);
    expect(result.current.hasMore).toBe(true);

    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toEqual([0, 1, 2, 3]));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toEqual([0, 1, 2, 3, 4]));
    expect(result.current.hasMore).toBe(false);
  });
});

describe('useQuery', () => {
  it('loads data and reload refetches', async () => {
    let value = 'a';
    const { result } = renderHook(() => useQuery(async () => value));
    await waitFor(() => expect(result.current.data).toBe('a'));
    value = 'b';
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toBe('b'));
  });

  it('captures errors', async () => {
    const { result } = renderHook(() =>
      useQuery(async () => {
        throw new Error('broken');
      }),
    );
    await waitFor(() => expect(result.current.error).toBe('broken'));
    expect(result.current.loading).toBe(false);
  });
});

describe('TruncationNotice', () => {
  it('renders nothing when everything is shown', () => {
    const { container } = render(
      <TruncationNotice shown={5} total={5} hasMore={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows counts and remaining, and fires load more', async () => {
    const onLoadMore = vi.fn();
    render(
      <TruncationNotice shown={500} total={12400} hasMore onLoadMore={onLoadMore} />,
    );
    expect(screen.getByText(/Showing 500 of 12,400/)).toBeInTheDocument();
    expect(screen.getByText(/11,900 more/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(onLoadMore).toHaveBeenCalledOnce();
  });

  it('marks approximate totals', () => {
    render(<TruncationNotice shown={10} total={100} approximate hasMore={false} />);
    expect(screen.getByText(/of ~100/)).toBeInTheDocument();
  });
});
