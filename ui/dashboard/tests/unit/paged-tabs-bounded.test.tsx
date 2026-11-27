/**
 * Issue #282 review — the Audit and Registry tabs grow only through Load more,
 * and both stop at a named display limit instead of growing without bound.
 */
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const audit = vi.fn();
const listAssets = vi.fn();
const getAsset = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  api: {
    audit: (...a: unknown[]) => audit(...a),
    listAssets: (...a: unknown[]) => listAssets(...a),
    getAsset: (...a: unknown[]) => getAsset(...a),
  },
}));

import AuditTab from '@/components/dashboard/tabs/AuditTab';
import { useAssetsPage } from '@/components/dashboard/tabs/registry/useAssetsPage';
import { AUDIT_ENTRIES_MAX, REGISTRY_ASSETS_MAX } from '@/lib/boundedList';

describe('AuditTab display limit', () => {
  beforeEach(() => audit.mockReset());

  it('stops Load more at AUDIT_ENTRIES_MAX and says so', async () => {
    let page = 0;
    audit.mockImplementation(async (limit: number) => {
      const entries = Array.from({ length: limit }, (_, i) => ({
        audit_id: `p${page}-${i}`, action: 'view', result: 'ok', ts: '2026-11-27T00:00:00Z',
      }));
      page += 1;
      return { entries, next_cursor: page * 1000, count: 100_000 };
    });
    const user = userEvent.setup();
    render(<AuditTab refreshTick={0} />);
    await waitFor(() => expect(audit).toHaveBeenCalledTimes(1));
    const pages = AUDIT_ENTRIES_MAX / 200;
    for (let i = 1; i < pages; i++) {
      await user.click(await screen.findByRole('button', { name: 'Load more' }));
      await waitFor(() => expect(audit).toHaveBeenCalledTimes(i + 1));
    }
    await waitFor(() => expect(screen.getByRole('note')).toHaveTextContent(
      `Showing the latest ${AUDIT_ENTRIES_MAX.toLocaleString()} entries (display limit)`,
    ));
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
    expect(audit).toHaveBeenCalledTimes(pages);
  });
});

describe('useAssetsPage display limit', () => {
  beforeEach(() => { listAssets.mockReset(); getAsset.mockReset(); });

  it('stops at REGISTRY_ASSETS_MAX with capped=true and hasMore=false', async () => {
    listAssets.mockImplementation(async ({ limit, offset }: { limit: number; offset: number }) => ({
      assets: Array.from({ length: limit }, (_, i) => ({ id: `a${offset + i}`, name: `n${offset + i}`, version: '1' })),
      total: 5000,
      has_more: true,
    }));
    getAsset.mockResolvedValue(null);
    const { result } = renderHook(() => useAssetsPage({ refreshTick: 0 }));
    await waitFor(() => expect(result.current.assets).toHaveLength(50));
    while ((result.current.assets?.length ?? 0) < REGISTRY_ASSETS_MAX) {
      const before = result.current.assets?.length ?? 0;
      await act(async () => { await result.current.loadMore(); });
      expect(result.current.assets!.length).toBeGreaterThan(before);
    }
    expect(result.current.assets).toHaveLength(REGISTRY_ASSETS_MAX);
    expect(result.current.capped).toBe(true);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.listMax).toBe(REGISTRY_ASSETS_MAX);
    const calls = listAssets.mock.calls.length;
    await act(async () => { await result.current.loadMore(); });
    expect(listAssets.mock.calls.length).toBe(calls);
  });
});
