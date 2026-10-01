/**
 * RunsTab reads its whole view from the URL (ADR-0232 D2): a pasted link with
 * `?f=` lists the filter's selection (not the cursor search), and `?run=` opens
 * the inspector on that run even before the list has loaded it.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const run = (id: string, status = 'error') => ({
  run_id: id, status, created_at: '2026-09-20T00:00:00Z', finished_at: null, duration_ms: 5,
  exit_code: 0, model_call_count: 0, tool_call_count: 0, mutating_tool_count: 0,
  command: ['python'], novafabric_version: null, capsule_path: `/c/${id}`,
});

const searchRuns = vi.fn(async () => ({ items: [run('LISTED', 'success')], next_cursor: null, total_approx: 1 }));
const filterRuns = vi.fn(async () => ({
  filter: 'status:error', scope: 'tree', where: 'status = error', cli_equivalent: 'nova query',
  matched: 1, truncated: false, complete: true, incomplete_reasons: [], since: '', until: '',
  items: [run('FILTERED')],
}));
const getRun = vi.fn(async (id: string) => ({
  run_id: id, capsule_path: `/c/${id}`, manifest: { run_id: id }, trace: [], model_calls: [],
  tool_calls: [], lineage: [], assets: [], inputs: [], outputs: [],
}));

vi.mock('@/lib/api', () => ({
  api: {
    searchRuns: (...a: unknown[]) => searchRuns(...(a as [])),
    filterRuns: (...a: unknown[]) => filterRuns(...(a as [])),
    getRun: (id: string) => getRun(id),
    suggestFilterValues: async () => ({ dimension: '', values: [], truncated: false, since: '', until: '' }),
  },
  FILTER_DIMENSIONS: ['status', 'model'],
  getConnection: () => ({ token: null, base: '' }),
  openManagedRunStream: () => ({ close: () => {} }),
  ServeApiError: class ServeApiError extends Error {},
}));

import RunsTab from '@/components/dashboard/tabs/RunsTab';

describe('RunsTab URL state', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('lists the filter selection for ?f= and honours ?scope=', async () => {
    window.history.replaceState({}, '', '/dashboard?tab=runs&f=status%3Aerror&scope=tree');
    render(<RunsTab onFlash={() => {}} refreshTick={0} />);
    // The list is virtualized (no layout in jsdom), so assert on the header
    // count and the summary rather than on rendered rows.
    expect(await screen.findByTestId('filter-summary')).toHaveTextContent('1 of 1 whole trees');
    expect(filterRuns).toHaveBeenCalledWith(expect.objectContaining({ f: 'status:error', scope: 'tree' }));
    expect(screen.getByText('Runs').parentElement).toHaveTextContent('(1)');
    expect(screen.getByRole('combobox', { name: 'Filter runs' })).toHaveValue('status:error');
  });

  it('opens the inspector on ?run= and fetches that capsule', async () => {
    window.history.replaceState({}, '', '/dashboard?tab=runs&run=DEEP&view=trace');
    render(<RunsTab onFlash={() => {}} refreshTick={0} />);
    await waitFor(() => expect(getRun).toHaveBeenCalledWith('DEEP'));
    expect(filterRuns).not.toHaveBeenCalled();
  });
});
