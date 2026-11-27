/**
 * #265 — the first-run panel is copy-only: it shows the exact command, copies it on
 * request, never executes or posts anything, and re-checks for runs on demand.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listRuns = vi.fn();
const getStats = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    listRuns: (...a: unknown[]) => listRuns(...a),
    getStats: (...a: unknown[]) => getStats(...a),
  },
}));

import OnboardingPanel from '@/components/dashboard/onboarding/OnboardingPanel';

const CAPSULE_DIR = '/home/me/.novafabric/capsules';

function command(): string {
  return screen.getByTestId('onboarding-command').textContent ?? '';
}

describe('OnboardingPanel', () => {
  const writeText = vi.fn();
  const fetchSpy = vi.fn(async () => { throw new Error('the panel must not call fetch directly'); });

  beforeEach(() => {
    listRuns.mockReset().mockResolvedValue({ runs: [], count: 0, total: 0, capsule_dir: CAPSULE_DIR });
    getStats.mockReset();
    writeText.mockReset().mockResolvedValue(undefined);
    fetchSpy.mockClear();
    vi.stubGlobal('fetch', fetchSpy);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('is a labelled region that states it never starts a capture', () => {
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    expect(screen.getByRole('region', { name: 'Capture your first run' })).toBeTruthy();
    expect(screen.getByText(/never starts a capture itself/)).toBeTruthy();
    expect(screen.getByText(/does not run this command and sends nothing to the server/)).toBeTruthy();
  });

  it('shows no sample or synthetic run', () => {
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    expect(screen.queryByText(/sample|synthetic|demo/i)).toBeNull();
  });

  it('builds the default command and adds --output-dir once the server names its capsule directory', async () => {
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    expect(command()).toBe('nova capture -- python agent.py');
    await waitFor(() => expect(command()).toBe(`nova capture --output-dir ${CAPSULE_DIR} -- python agent.py`));
    fireEvent.click(screen.getByLabelText(/Write the capsule where this server reads/));
    expect(command()).toBe('nova capture -- python agent.py');
  });

  it('does not offer --output-dir when the capsule directory is unknown', async () => {
    listRuns.mockRejectedValue(new Error('503'));
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    await waitFor(() => expect(listRuns).toHaveBeenCalled());
    expect(screen.queryByLabelText(/Write the capsule where this server reads/)).toBeNull();
    expect(command()).toBe('nova capture -- python agent.py');
  });

  it('labels every control and updates the command from the workload and capture level', async () => {
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    await waitFor(() => expect(command()).toContain('--output-dir'));
    fireEvent.change(screen.getByLabelText('Workload'), { target: { value: 'uv' } });
    expect((screen.getByLabelText('Command to capture') as HTMLInputElement).value).toBe('uv run python agent.py');
    fireEvent.click(screen.getByRole('radio', { name: 'forensic' }));
    expect(command()).toBe(`NOVA_CAPTURE_LEVEL=forensic nova capture --output-dir ${CAPSULE_DIR} -- uv run python agent.py`);
    expect(screen.getByLabelText(/Timeout in seconds/)).toBeTruthy();
    expect(screen.getByLabelText(/Environment tag/)).toBeTruthy();
  });

  it('explains a refused command, marks the field invalid and disables Copy', () => {
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    const input = screen.getByLabelText('Command to capture');
    fireEvent.change(input, { target: { value: 'python a.py | tee log' } });
    expect(screen.queryByTestId('onboarding-command')).toBeNull();
    expect(screen.getByText(/shell syntax/)).toBeTruthy();
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect((screen.getByRole('button', { name: 'Copy capture command' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('copies exactly the shown command and announces it', async () => {
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    await waitFor(() => expect(command()).toContain('--output-dir'));
    fireEvent.click(screen.getByRole('button', { name: 'Copy capture command' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(command()));
    expect(await screen.findByText(/Command copied/)).toBeTruthy();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('falls back to manual copy when the clipboard is unavailable', async () => {
    writeText.mockRejectedValue(new Error('denied'));
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    // Wait for the capsule directory first: a changed command clears the copy message.
    await waitFor(() => expect(command()).toContain('--output-dir'));
    fireEvent.click(screen.getByRole('button', { name: 'Copy capture command' }));
    expect(await screen.findByText(/Clipboard unavailable/)).toBeTruthy();
  });

  it('reports runs found after a re-check', async () => {
    getStats.mockResolvedValue({ run_count: 2 });
    const onRunsFound = vi.fn();
    render(<OnboardingPanel onRunsFound={onRunsFound} onNavigate={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check for runs' }));
    await waitFor(() => expect(onRunsFound).toHaveBeenCalledWith(2));
    expect(screen.getByText('2 runs found.')).toBeTruthy();
  });

  it('says where it looked when there are still no runs', async () => {
    getStats.mockResolvedValue({ run_count: 0 });
    const onRunsFound = vi.fn();
    render(<OnboardingPanel onRunsFound={onRunsFound} onNavigate={() => {}} />);
    await waitFor(() => expect(command()).toContain('--output-dir'));
    fireEvent.click(screen.getByRole('button', { name: 'Check for runs' }));
    expect(await screen.findByText(`No runs yet in ${CAPSULE_DIR}.`)).toBeTruthy();
    expect(onRunsFound).not.toHaveBeenCalled();
  });

  it('does not read a refused run count as zero', async () => {
    getStats.mockResolvedValue({ run_count: null });
    const onRunsFound = vi.fn();
    render(<OnboardingPanel onRunsFound={onRunsFound} onNavigate={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check for runs' }));
    expect(await screen.findByText(/could not count runs/)).toBeTruthy();
    expect(screen.queryByText(/No runs yet/)).toBeNull();
    expect(onRunsFound).not.toHaveBeenCalled();
  });

  it('shows the error when the re-check fails', async () => {
    getStats.mockRejectedValue(new Error('503 Service Unavailable'));
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check for runs' }));
    expect(await screen.findByText(/Could not check: 503 Service Unavailable/)).toBeTruthy();
  });

  it('navigates to the Runs and Diff tabs', () => {
    const onNavigate = vi.fn();
    render(<OnboardingPanel onRunsFound={() => {}} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Runs tab' }));
    fireEvent.click(screen.getByRole('button', { name: 'Diff tab' }));
    expect(onNavigate.mock.calls).toEqual([['runs'], ['diff']]);
  });
});
