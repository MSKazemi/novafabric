/**
 * EU AI Act Art.12 governance panels: the retention figures must match the
 * corrected semantics (issue #63). The log floor is six months (Art. 19(1)
 * providers, Art. 26(6) deployers); the 10-year figure is the provider's
 * documentation horizon (Art. 18(1)), applied to capsules as policy, not a log duty.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import EuAiActStatusPanel from '@/components/dashboard/tabs/governance/EuAiActStatusPanel';
import EuAiActExportPanel from '@/components/dashboard/tabs/governance/EuAiActExportPanel';

const status = vi.fn();
const exportLogs = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    euaiactStatus: (...a: unknown[]) => status(...a),
    euaiactExport: (...a: unknown[]) => exportLogs(...a),
  },
}));

const DEPLOYER = {
  ok: true, high_risk: true, provider_mode: false, retention_months: 6,
  log_retention_floor_months: 6, documentation_retention_months: null,
  deadline: '2026-08-02', note: '',
};
const PROVIDER = {
  ...DEPLOYER, provider_mode: true, retention_months: 120,
  documentation_retention_months: 120,
};

function text(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ');
}

beforeEach(() => {
  status.mockReset();
  exportLogs.mockReset();
});

describe('EuAiActStatusPanel retention', () => {
  it('labels the six-month floor with Art. 19(1) / Art. 26(6) in deployer mode', async () => {
    status.mockResolvedValue(DEPLOYER);
    render(<EuAiActStatusPanel />);
    await screen.findByText('Log retention floor');
    expect(text()).toContain('6 months (Art. 19(1) providers / Art. 26(6) deployers)');
    expect(screen.queryByText('Documentation horizon')).toBeNull();
    expect(text()).not.toContain('Art.18 provider');
    expect(text()).not.toContain('Art.12 deployer');
  });

  it('keeps the floor at 6 and reports 120 as the Art. 18(1) documentation horizon', async () => {
    status.mockResolvedValue(PROVIDER);
    render(<EuAiActStatusPanel />);
    await screen.findByText('Documentation horizon');
    expect(text()).toContain('6 months (Art. 19(1) providers / Art. 26(6) deployers)');
    expect(text()).toContain('120 months (Art. 18(1) documentation, not logs');
    expect(text()).not.toContain('120 months (Art.18');
    expect(text()).not.toContain('Retention floor 120');
  });

  it('keeps the Art.50 deadline label', async () => {
    status.mockResolvedValue(DEPLOYER);
    render(<EuAiActStatusPanel />);
    await screen.findByText('Art.50 deadline');
    expect(text()).toContain('2026-08-02');
  });
});

describe('EuAiActExportPanel retention', () => {
  async function runExport(body: Record<string, unknown>) {
    exportLogs.mockResolvedValue({ ok: true, records: [], count: 0, mode: 'deployer', ...body });
    render(<EuAiActExportPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Export' }));
    await waitFor(() => expect(exportLogs).toHaveBeenCalled());
    await screen.findByText(/Log retention floor/);
  }

  it('shows the log floor with its provisions, not the applied retention', async () => {
    await runExport({
      retention_months: 6, log_retention_floor_months: 6,
      documentation_retention_months: null,
    });
    expect(text()).toContain('Log retention floor: 6 months (Art. 19(1) / Art. 26(6))');
    expect(text()).not.toContain('Documentation horizon');
  });

  it('does not present the provider 120 months as the floor', async () => {
    await runExport({
      mode: 'provider', retention_months: 120, log_retention_floor_months: 6,
      documentation_retention_months: 120,
    });
    expect(text()).toContain('Log retention floor: 6 months (Art. 19(1) / Art. 26(6))');
    expect(text()).toContain('Documentation horizon applied: 120 months (Art. 18(1), provider mode)');
    expect(text()).not.toContain('Retention floor: 120');
  });
});
