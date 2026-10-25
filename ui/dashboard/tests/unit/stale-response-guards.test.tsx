/**
 * Reviews frontend-dashboard-11 and -12 — a response that lands after the question changed
 * must not be shown as the answer to the new question.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/lib/ToastContext';
import { useMutation } from '@/lib/useMutation';

const searchRuns = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  api: { searchRuns: (...a: unknown[]) => searchRuns(...a) },
  getConnection: () => ({ token: null, base: 'http://127.0.0.1:4321' }),
  openManagedRunStream: () => ({ close: () => {} }),
}));

import { useRunSearch } from '@/components/dashboard/tabs/runs/useRunSearch';

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

const page = (ids: string[], cursor: string | null = null) => ({
  items: ids.map((run_id) => ({ run_id })),
  next_cursor: cursor,
  total_approx: ids.length,
});

describe('useRunSearch', () => {
  beforeEach(() => searchRuns.mockReset());

  const props = (search: string) => ({
    search, statusFilter: 'all' as const, since: '', until: '', refreshTick: 0,
  });

  it('shows the results of the latest query when responses arrive out of order', async () => {
    const a = deferred<ReturnType<typeof page>>();
    const ab = deferred<ReturnType<typeof page>>();
    searchRuns.mockReturnValueOnce(a.promise).mockReturnValueOnce(ab.promise);
    const { result, rerender } = renderHook((p) => useRunSearch(p), { initialProps: props('a') });
    rerender(props('ab'));
    await act(async () => { ab.resolve(page(['for-ab'])); });
    await waitFor(() => expect(result.current.runs?.map((r) => r.run_id)).toEqual(['for-ab']));
    await act(async () => { a.resolve(page(['for-a'])); });
    expect(result.current.runs?.map((r) => r.run_id)).toEqual(['for-ab']);
  });

  it('does not append a page fetched under the old filter to the new filter\'s list', async () => {
    searchRuns.mockResolvedValueOnce(page(['old-1'], 'cur-1'));
    const { result, rerender } = renderHook((p) => useRunSearch(p), { initialProps: props('old') });
    await waitFor(() => expect(result.current.runs?.map((r) => r.run_id)).toEqual(['old-1']));

    const oldPage2 = deferred<ReturnType<typeof page>>();
    searchRuns.mockReturnValueOnce(oldPage2.promise).mockResolvedValueOnce(page(['new-1']));
    let more!: Promise<void>;
    act(() => { more = result.current.loadMore(); });
    rerender(props('new'));
    await waitFor(() => expect(result.current.runs?.map((r) => r.run_id)).toEqual(['new-1']));
    await act(async () => { oldPage2.resolve(page(['old-2'])); await more; });
    expect(result.current.runs?.map((r) => r.run_id)).toEqual(['new-1']);
  });
});

describe('useMutation', () => {
  const wrapper = ({ children }: { children: ReactNode }) => <ToastProvider>{children}</ToastProvider>;

  it('clears the previous result when a new run starts, so a failed run shows no stale verdict', async () => {
    let fail = false;
    const { result } = renderHook(
      () => useMutation(async () => { if (fail) throw new Error('boom'); return 'first'; }, { silentSuccess: true, silentError: true }),
      { wrapper },
    );
    await act(async () => { await result.current.run(); });
    expect(result.current.result).toBe('first');
    fail = true;
    await act(async () => { await result.current.run(); });
    expect(result.current.error).toBe('boom');
    expect(result.current.result).toBeNull();
  });
});

describe('IntegrityPanel', () => {
  it("drops run A's verdict when the user switched to run B before it returned", async () => {
    const { render, screen, fireEvent } = await import('@testing-library/react');
    const { IntegrityPanel } = await import('@/components/dashboard/tabs/runs/ExplorerPanels');
    const pending = deferred<never>();
    const verify = vi.fn(() => pending.promise);
    const { rerender } = render(<IntegrityPanel runId="run-A" verify={verify} />);
    fireEvent.click(screen.getByRole('button', { name: /verify seal/i }));
    rerender(<IntegrityPanel runId="run-B" verify={verify} />);
    await act(async () => {
      pending.resolve({ sealed: true, valid: true, signature_ok: true, timestamp_ok: true, log_integrity_ok: true } as never);
    });
    expect(screen.queryByText('Seal verified')).toBeNull();
    expect(screen.getByText('Not verified in this session.')).toBeTruthy();
  });
});
