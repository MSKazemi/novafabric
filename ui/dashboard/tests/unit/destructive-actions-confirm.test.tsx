/**
 * Review frontend-dashboard-13 — irreversible actions ran on one keystroke or click, and a
 * legal-hold refusal showed only "409 Conflict".
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const erasureRequest = vi.fn();
const piiErase = vi.fn();
const releaseHold = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  api: {
    erasureRequest: (...a: unknown[]) => erasureRequest(...a),
    erasureStatus: vi.fn(),
    piiErase: (...a: unknown[]) => piiErase(...a),
    releaseHold: (...a: unknown[]) => releaseHold(...a),
    listHolds: vi.fn(async () => ({
      total_active: 1,
      registries: [{
        name: 'main',
        holds: [{
          hold_id: 'hold-77', registry: 'main', reason: 'litigation', duration_days: null,
          created_at: '2026-10-01T00:00:00', released_at: null,
        }],
      }],
    })),
    listAssets: vi.fn(async () => ({ assets: [] })),
  },
}));

import GdprErasurePanel from '@/components/dashboard/tabs/compliance/GdprErasurePanel';
import PiiErasePanel from '@/components/dashboard/tabs/compliance/PiiErasePanel';

beforeEach(() => {
  erasureRequest.mockReset();
  piiErase.mockReset();
  releaseHold.mockReset();
  try { localStorage.clear(); } catch { /* jsdom */ }
});

describe('GdprErasurePanel', () => {
  it('Enter in the subject field asks for confirmation and calls nothing', () => {
    render(<GdprErasurePanel runIds={[]} />);
    const input = screen.getByPlaceholderText('subject-001 or run_...');
    fireEvent.change(input, { target: { value: 'subject-9' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(erasureRequest).not.toHaveBeenCalled();
  });

  it('erases only after the confirmation, naming the subject', async () => {
    erasureRequest.mockResolvedValue({
      erased: true, cap003_enabled: true,
      request: { state: 'COMPLETED', receipt_sha256: 'abc' },
    });
    render(<GdprErasurePanel runIds={[]} />);
    fireEvent.change(screen.getByPlaceholderText('subject-001 or run_...'), { target: { value: 'subject-9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Erase now…' }));
    expect(erasureRequest).not.toHaveBeenCalled();
    expect(screen.getByText('subject-9', { selector: 'code' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Erase now' }));
    await waitFor(() => expect(erasureRequest).toHaveBeenCalledWith('subject-9', 'gdpr_art_17'));
  });

  it('Cancel leaves the data alone', () => {
    render(<GdprErasurePanel runIds={[]} />);
    fireEvent.change(screen.getByPlaceholderText('subject-001 or run_...'), { target: { value: 'subject-9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Erase now…' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(erasureRequest).not.toHaveBeenCalled();
  });
});

describe('PiiErasePanel', () => {
  it('destroys the key only after the confirmation', async () => {
    piiErase.mockResolvedValue({ ok: true, erased: true, receipt: {} });
    render(<PiiErasePanel runIds={[]} />);
    fireEvent.change(screen.getByPlaceholderText('data subject identifier'), { target: { value: 'subj-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Erase DEK…' }));
    expect(piiErase).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Erase DEK' }));
    await waitFor(() => expect(piiErase).toHaveBeenCalledTimes(1));
  });
});

describe('legal-hold release', () => {
  it('asks before releasing, naming the hold', async () => {
    releaseHold.mockResolvedValue({ ok: true });
    const { default: HoldsTab } = await import('@/components/dashboard/tabs/HoldsTab');
    render(<HoldsTab refreshTick={0} />);
    fireEvent.click(await screen.findByRole('button', { name: 'release' }));
    expect(releaseHold).not.toHaveBeenCalled();
    expect(within(screen.getByRole('dialog')).getByText('hold-77')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Release hold' }));
    await waitFor(() => expect(releaseHold).toHaveBeenCalledWith('hold-77'));
  });
});
