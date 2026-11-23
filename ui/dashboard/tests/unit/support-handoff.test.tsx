import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SupportHandoff from '@/components/dashboard/SupportHandoff';
import ConnectPanel from '@/components/dashboard/ConnectPanel';
import DoctorPanel from '@/components/dashboard/tabs/admin/DoctorPanel';

const { doctorMock } = vi.hoisted(() => ({ doctorMock: vi.fn() }));
vi.mock('@/lib/api', () => ({
  ping: vi.fn(), validateToken: vi.fn(), setConnection: vi.fn(),
  api: { doctor: doctorMock },
}));

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor);
  else Reflect.deleteProperty(navigator, 'clipboard');
});

describe('local support handoff', () => {
  it('copies only a static CLI command, without leaking a token or sending diagnostics', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    try {
      render(<SupportHandoff context="disconnected" />);
      fireEvent.click(screen.getByRole('button', { name: 'Copy nova support-bundle' }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith('nova support-bundle'));
      await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('nova support-bundle copied.'));
      expect(fetchMock).not.toHaveBeenCalled();
      expect(screen.getByText(/nothing is uploaded automatically/i)).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('provides a manual-copy fallback when the Clipboard API is unavailable', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    render(<SupportHandoff context="health" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy nova doctor' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Select the command text'));
  });

  it('shows offline guidance when the connection panel has an error', () => {
    render(<ConnectPanel bootError="Server is unreachable" setBootError={vi.fn()} onConnected={vi.fn()} />);
    expect(screen.getByRole('region', { name: 'Local troubleshooting' })).toBeInTheDocument();
    expect(screen.getByText(/Server is unreachable/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy nova doctor' })).toBeInTheDocument();
  });

  it('shows a support handoff after a real failed doctor result', async () => {
    doctorMock.mockResolvedValue({ checks: [{ name: 'registry', ok: false, detail: 'unavailable' }] });
    render(<DoctorPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'nova doctor' }));
    expect(await screen.findByRole('region', { name: 'Local troubleshooting' })).toBeInTheDocument();
    expect(screen.getByText('unavailable')).toBeInTheDocument();
  });

  it('shows the troubleshooting handoff when the doctor request is refused', async () => {
    doctorMock.mockRejectedValue(new Error('access denied'));
    render(<DoctorPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'nova doctor' }));
    expect(await screen.findByRole('region', { name: 'Local troubleshooting' })).toBeInTheDocument();
    expect(screen.getByText('access denied')).toBeInTheDocument();
  });

  it('does not show troubleshooting on an idle connection form', () => {
    render(<ConnectPanel bootError={null} setBootError={vi.fn()} onConnected={vi.fn()} />);
    expect(screen.queryByRole('region', { name: 'Local troubleshooting' })).not.toBeInTheDocument();
  });

  it('clears stale failed-check guidance while refreshing the doctor result', async () => {
    const failed = { checks: [{ name: 'registry', ok: false, detail: 'unavailable' }] };
    const recovered = { checks: [{ name: 'registry', ok: true, detail: 'ready' }] };
    let resolveRecheck: (value: typeof recovered) => void = () => {};
    const recheck = new Promise<typeof recovered>((resolve) => { resolveRecheck = resolve; });
    doctorMock.mockResolvedValueOnce(failed).mockReturnValueOnce(recheck);

    render(<DoctorPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'nova doctor' }));
    expect(await screen.findByRole('region', { name: 'Local troubleshooting' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'nova doctor' }));
    expect(screen.getByRole('button', { name: 'running…' })).toBeDisabled();
    expect(screen.queryByRole('region', { name: 'Local troubleshooting' })).not.toBeInTheDocument();
    resolveRecheck(recovered);
    await screen.findByText('ready');
    expect(screen.queryByRole('region', { name: 'Local troubleshooting' })).not.toBeInTheDocument();
  });

  it('does not hide a failed overall doctor verdict when individual checks are empty', async () => {
    doctorMock.mockResolvedValue({ ok: false, checks: [] });
    render(<DoctorPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'nova doctor' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('overall diagnostic check reported a failure');
    expect(screen.getByRole('region', { name: 'Local troubleshooting' })).toBeInTheDocument();
  });

  it('does not show a failure handoff when doctor reports all checks healthy', async () => {
    doctorMock.mockResolvedValue({ checks: [{ name: 'registry', ok: true, detail: 'ready' }] });
    render(<DoctorPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'nova doctor' }));
    await screen.findByText('ready');
    expect(screen.queryByRole('region', { name: 'Local troubleshooting' })).not.toBeInTheDocument();
  });
});
