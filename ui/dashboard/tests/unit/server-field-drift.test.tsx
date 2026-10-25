/** Review frontend-dashboard-19 — the dashboard read names the server does not send, and drew "not checked" as FAIL. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const classifyManual = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  api: { classifyManual: (...a: unknown[]) => classifyManual(...a) },
}));

import ManualClassifyPanel from '@/components/dashboard/tabs/governance/ManualClassifyPanel';
import { IntegrityPanel } from '@/components/dashboard/tabs/runs/ExplorerPanels';

describe('ManualClassifyPanel', () => {
  it("shows the server's nist_rmf_impact and omb_flags", async () => {
    classifyManual.mockResolvedValue({
      classification: {
        eu_ai_act_tier: 'high_risk', nist_rmf_impact: 'high', omb_flags: ['rights-impacting'],
        rationale: 'because',
      },
    });
    render(<ManualClassifyPanel />);
    fireEvent.change(screen.getByPlaceholderText('my-ai-system'), { target: { value: 'sys' } });
    fireEvent.change(screen.getByPlaceholderText(/What the system does/), { target: { value: 'does things' } });
    fireEvent.click(screen.getByRole('button', { name: /classify/i }));
    await waitFor(() => expect(screen.getByText(/NIST RMF/)).toBeTruthy());
    expect(screen.getByText(/OMB M-24-10: rights-impacting/)).toBeTruthy();
  });
});

describe('IntegrityPanel', () => {
  it('draws a timestamp that was not checked as "not checked", not as failed', async () => {
    const verify = vi.fn(async () => ({
      sealed: true, valid: true, configured: true,
      signature_ok: true, timestamp_ok: null, log_integrity_ok: true,
    }) as never);
    render(<IntegrityPanel runId="r1" verify={verify} />);
    fireEvent.click(screen.getByRole('button', { name: /verify seal/i }));
    await waitFor(() => expect(screen.getByText('not checked')).toBeTruthy());
    expect(screen.queryByText('failed')).toBeNull();
  });
});
