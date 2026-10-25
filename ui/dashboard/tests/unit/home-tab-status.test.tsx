/**
 * Review frontend-dashboard-14 — the Home tab showed a constant "0 violations" (green) and
 * "Registry OK" whatever the server state ("absent is not zero", ADR-0234).
 */
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getStats = vi.fn();
const listEvidence = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    getStats: (...a: unknown[]) => getStats(...a),
    listRuns: vi.fn(async () => ({ runs: [] })),
    listEvidence: (...a: unknown[]) => listEvidence(...a),
  },
  getConnection: () => ({ token: null, base: 'http://127.0.0.1:4321' }),
}));

import HomeTab from '@/components/dashboard/tabs/HomeTab';

const stats = {
  run_count: 3, failed_run_count: 0, passed_run_count: 3,
  asset_count: 1, pending_eval_count: 0, production_asset_count: 0,
};

describe('HomeTab status', () => {
  beforeEach(() => {
    getStats.mockReset();
    listEvidence.mockReset();
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it('never shows a constant "0 violations" and counts unverified bundles instead', async () => {
    getStats.mockResolvedValue(stats);
    listEvidence.mockResolvedValue({
      total: 9, count: 9,
      bundles: [{ verified: true }, { verified: true }, { verified: false }],
    });
    render(<HomeTab onNavigate={() => {}} />);
    await waitFor(() => expect(screen.getByText('unverified')).toBeTruthy());
    expect(screen.queryByText('violations')).toBeNull();
    expect(screen.getByText('7')).toBeTruthy(); // total 9 - verified 2
  });

  it('says the registry is reachable only after the stats call answered', async () => {
    getStats.mockResolvedValue(stats);
    listEvidence.mockResolvedValue({ total: 0, count: 0, bundles: [] });
    render(<HomeTab onNavigate={() => {}} />);
    await waitFor(() => expect(screen.getByText('Registry reachable')).toBeTruthy());
    expect(screen.queryByText('Registry OK')).toBeNull();
  });

  it('does not claim a healthy registry when the stats call fails', async () => {
    getStats.mockRejectedValue(new Error('503 Service Unavailable'));
    listEvidence.mockResolvedValue({ total: 0, count: 0, bundles: [] });
    render(<HomeTab onNavigate={() => {}} />);
    await waitFor(() => expect(screen.getByText(/503 Service Unavailable/)).toBeTruthy());
    expect(screen.queryByText('Registry reachable')).toBeNull();
    expect(screen.queryByText('Registry OK')).toBeNull();
  });
});
