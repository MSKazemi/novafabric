/**
 * Review frontend-dashboard-07 (second half) — a refused cost report (`ok:false`, the
 * total withheld as "absent is not zero", ADR-0234) threw `.toFixed` on null and
 * crashed the tab; the per-model chart looked for `model` / `model_name` / `name`
 * while the server's row key is `model_id`, so it had no x axis.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const costReport = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    costPricing: vi.fn(async () => ({ pricing: [], source: 's', note: '' })),
    listRuns: vi.fn(async () => ({ runs: [] })),
    costReport: (...a: unknown[]) => costReport(...a),
  },
}));

import CostTab from '@/components/dashboard/tabs/CostTab';

describe('CostTab', () => {
  beforeEach(() => costReport.mockReset());

  it('renders a refused report as n/a instead of crashing', async () => {
    costReport.mockResolvedValue({
      ok: true, backend: 'clickhouse', run_id: null, days: 7, note: '',
      totals: { input_tokens: 10, completion_tokens: 5, cost_usd: null },
      by_model: [],
    });
    render(<CostTab />);
    await waitFor(() => expect(screen.getByText(/backend: clickhouse/)).toBeTruthy());
    expect(screen.getByText('n/a')).toBeTruthy();
    expect(screen.queryByText(/\$null/)).toBeNull();
  });

  it('does not crash when the whole totals block is withheld', async () => {
    costReport.mockResolvedValue({
      ok: false, backend: 'none', run_id: null, days: 7, note: '', reason: 'no data',
      totals: null, by_model: [],
    });
    render(<CostTab />);
    await waitFor(() => expect(screen.getByText(/backend: none/)).toBeTruthy());
    expect(screen.getAllByText('n/a').length).toBeGreaterThan(0);
  });

  it('still shows a priced total', async () => {
    costReport.mockResolvedValue({
      ok: true, backend: 'sqlite', run_id: null, days: 7, note: '',
      totals: { input_tokens: 1200, completion_tokens: 5, cost_usd: 0.015 },
      by_model: [],
    });
    render(<CostTab />);
    await waitFor(() => expect(screen.getByText('$0.0150')).toBeTruthy());
  });
});
