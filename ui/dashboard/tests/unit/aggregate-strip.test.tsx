/**
 * ADR-0234 — the Runs aggregate strip and the shared refusal rendering.
 *
 * The strip forwards the Runs view state so the *server* decides whether the
 * aggregate is faithful; on a refusal it shows reason + remedy and disables
 * its metric toggles with the reason attached — never bars for a different
 * population than the list.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const analyticsSummary = vi.fn();

vi.mock('@/lib/api', () => ({
  api: { analyticsSummary: (...a: unknown[]) => analyticsSummary(...a) },
}));

import AggregateStrip from '@/components/dashboard/tabs/runs/AggregateStrip';
import AggregateRefusal from '@/components/dashboard/AggregateRefusal';

const bucket = (day: string, runs: number, failed: number, p95: number | null, n: number) => ({
  bucket: day, run_count: runs, failed_count: failed, model_call_count: 0, tool_call_count: 0,
  duration_ms_p50: p95, duration_ms_p95: p95, duration_ms_max: p95, duration_samples: n,
});

const props = { since: '', until: '', filterText: '', statusFilter: 'all', search: '', refreshTick: 0 };

describe('AggregateStrip', () => {
  beforeEach(() => { analyticsSummary.mockReset(); });

  it('draws the computable aggregate and forwards the view state', async () => {
    analyticsSummary.mockResolvedValue({
      buckets: [bucket('2026-07-14', 2, 1, 300, 2), bucket('2026-07-15', 1, 0, 200, 1)],
      totals: { run_count: 3, failed_count: 1, model_call_count: 0, tool_call_count: 0 },
      since: null, until: null,
      aggregate: { computable: true, value: {}, notes: { source: 'runs_cache' } },
    });
    render(<AggregateStrip {...props} since="2026-07-01" />);
    expect(await screen.findByTestId('aggregate-totals')).toHaveTextContent('3 runs · 1 failed');
    expect(analyticsSummary).toHaveBeenCalledWith(expect.objectContaining({ since: '2026-07-01', status: 'all' }));
    expect(screen.getByRole('button', { name: 'runs' })).toBeEnabled();
  });

  it('renders a refusal, not numbers, and disables the toggles with the reason', async () => {
    analyticsSummary.mockResolvedValue({
      buckets: [], totals: null, since: null, until: null,
      aggregate: {
        computable: false, condition: 'unpushable_filter',
        reason: 'the run aggregate can only be narrowed by date; the view is also narrowed by filter',
        remedy: 'clear the filter to see aggregates',
      },
    });
    render(<AggregateStrip {...props} filterText="status:error" />);
    const refusal = await screen.findByTestId('aggregate-strip-refusal');
    expect(refusal).toHaveTextContent('filter not applicable');
    expect(refusal).toHaveTextContent('clear the filter');
    expect(analyticsSummary).toHaveBeenCalledWith(expect.objectContaining({ f: 'status:error' }));
    const runs = screen.getByRole('button', { name: 'runs' });
    expect(runs).toBeDisabled();
    expect(runs).toHaveAttribute('title', expect.stringContaining('narrowed by filter'));
    expect(screen.queryByTestId('aggregate-totals')).toBeNull();
  });

  it('says so when the request itself fails', async () => {
    analyticsSummary.mockRejectedValue(new Error('500 boom'));
    render(<AggregateStrip {...props} />);
    await waitFor(() => expect(screen.getByTestId('aggregate-strip')).toHaveTextContent('Aggregates unavailable: 500 boom'));
  });
});

describe('AggregateRefusal', () => {
  it('renders nothing for a computable verdict', () => {
    const { container } = render(<AggregateRefusal verdict={{ computable: true, value: 1 }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the condition, reason and remedy', () => {
    render(<AggregateRefusal verdict={{
      computable: false, condition: 'truncated_source', reason: 'index holds 2 of 4', remedy: 'reindex',
    }} />);
    const el = screen.getByTestId('aggregate-refusal');
    expect(el).toHaveAttribute('data-condition', 'truncated_source');
    expect(el).toHaveTextContent('partial data');
    expect(el).toHaveTextContent('index holds 2 of 4');
    expect(el).toHaveTextContent('reindex');
  });
});
