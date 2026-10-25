/** Review frontend-dashboard-10 — the browser could tick "NovaSeal-verified (confidence = 1.0)" for any capsule. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const kgIngest = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  api: { kgIngest: (...a: unknown[]) => kgIngest(...a) },
}));

import KGIngestPanel from '@/components/dashboard/tabs/kg/KGIngestPanel';

describe('KGIngestPanel', () => {
  it('offers no verified checkbox and sends only the capsule path', async () => {
    kgIngest.mockResolvedValue({ ok: true, ingested: 3, written: 2, skipped: 0, novaseal_verified: false, note: '' });
    render(<KGIngestPanel capsuleDirs={[]} />);
    expect(screen.queryByRole('checkbox')).toBeNull();
    fireEvent.change(screen.getByPlaceholderText(/run_id/), { target: { value: 'run-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }));
    await waitFor(() => expect(kgIngest).toHaveBeenCalledWith('run-1'));
    expect(await screen.findByText(/not NovaSeal-verified/)).toBeTruthy();
  });

  it("reports the server's verdict when the seal verifies", async () => {
    kgIngest.mockResolvedValue({ ok: true, ingested: 1, written: 1, skipped: 0, novaseal_verified: true, note: '' });
    render(<KGIngestPanel capsuleDirs={[]} />);
    fireEvent.change(screen.getByPlaceholderText(/run_id/), { target: { value: 'run-2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }));
    expect(await screen.findByText(/· NovaSeal-verified/)).toBeTruthy();
  });
});
