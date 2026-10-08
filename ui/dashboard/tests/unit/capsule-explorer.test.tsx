/**
 * Capsule explorer panels (Runs inspector).
 *
 * Acceptance criteria pinned here:
 * - The summary shows status, timing and call counts, and offers replay /
 *   export actions that route through the existing confirm flow (onAction).
 * - Seal verification distinguishes four outcomes honestly: not sealed,
 *   sealed-but-unverifiable, verified, failed (with the failing checks named).
 * - A verdict never carries over to a different run.
 * - Lineage lists neighbours in both directions; a neighbour opens by id and
 *   can be compared; no edges is an explained empty state, not a blank.
 * - The view switcher is a keyboard-navigable tablist.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { CapsuleSummary, IntegrityPanel, LineageNeighbours } from '@/components/dashboard/tabs/runs/ExplorerPanels';
import RunInspector from '@/components/dashboard/tabs/runs/RunInspector';
import type { CapsuleVerifyResult, FullCapsule, RunSummary } from '@/lib/api';

const run: RunSummary = {
  run_id: 'R1', status: 'error', created_at: '2026-09-20T10:00:00Z', finished_at: null,
  duration_ms: 2500, exit_code: 1, model_call_count: 2, tool_call_count: 3, mutating_tool_count: 1,
  command: ['python', 'agent.py'], novafabric_version: '0.102.1', capsule_path: '/c/R1',
};
const capsule = {
  run_id: 'R1', capsule_path: '/c/R1', manifest: { run_id: 'R1' }, trace: [],
  model_calls: [{}, {}], tool_calls: [{}, {}, {}], lineage: [], assets: [], inputs: [], outputs: [],
} as unknown as FullCapsule;

describe('CapsuleSummary', () => {
  it('shows the facts and routes actions through onAction', async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(<CapsuleSummary run={run} capsule={capsule} onAction={onAction} />);
    const summary = screen.getByRole('region', { name: 'Run summary' });
    expect(summary).toHaveTextContent('2.5 s');
    expect(summary).toHaveTextContent('3 (1 mutating)');
    expect(summary).toHaveTextContent('$ python agent.py');
    await user.click(screen.getByRole('button', { name: 'Replay dry-run' }));
    await user.click(screen.getByRole('button', { name: 'Export evidence' }));
    expect(onAction.mock.calls.map(c => c[1])).toEqual(['dry-run', 'export']);
  });
});

describe('IntegrityPanel', () => {
  async function verdictFor(result: CapsuleVerifyResult): Promise<HTMLElement> {
    const user = userEvent.setup();
    render(<IntegrityPanel runId="R1" verify={async () => result} />);
    expect(screen.getByText('Not verified in this session.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Verify seal' }));
    return screen.findByTestId('integrity-verdict');
  }

  it('says "not sealed" rather than pass or fail', async () => {
    expect(await verdictFor({ sealed: false, configured: null, message: 'No .seal/ directory' }))
      .toHaveTextContent('Not sealed.');
  });

  it('says a sealed capsule is unverifiable here when the verifier is not configured', async () => {
    expect(await verdictFor({ sealed: true, configured: false, message: 'NovaSeal not configured' }))
      .toHaveTextContent('Sealed, but not verifiable here.');
  });

  it('names the failing checks', async () => {
    const v = await verdictFor({
      sealed: true, configured: true, signature_ok: true, timestamp_ok: false,
      log_integrity_ok: true, valid: false, errors: ['timestamp mismatch'],
    });
    expect(v).toHaveTextContent('FAILED');
    expect(screen.getByText('RFC 3161 timestamp').parentElement).toHaveTextContent('failed');
    expect(screen.getByText('timestamp mismatch')).toBeInTheDocument();
  });

  it('reports a pass', async () => {
    expect(await verdictFor({
      sealed: true, configured: true, signature_ok: true, timestamp_ok: true,
      log_integrity_ok: true, valid: true,
    })).toHaveTextContent('Seal verified');
  });

  it('drops the verdict when the run changes', async () => {
    const user = userEvent.setup();
    const verify = async () => ({ sealed: false, configured: null } as CapsuleVerifyResult);
    const { rerender } = render(<IntegrityPanel runId="R1" verify={verify} />);
    await user.click(screen.getByRole('button', { name: 'Verify seal' }));
    await screen.findByTestId('integrity-verdict');
    rerender(<IntegrityPanel runId="R2" verify={verify} />);
    expect(screen.queryByTestId('integrity-verdict')).toBeNull();
  });
});

describe('LineageNeighbours', () => {
  it('lists both directions, opens and compares neighbours', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const onCompareTo = vi.fn();
    const load = async () => ({
      count: 2,
      edges: [
        { source_run_id: 'R1', target_run_id: 'CHILD', edge_type: 'contains' },
        { source_run_id: 'UP', target_run_id: 'R1', edge_type: 'derived_from' },
      ],
    });
    render(<LineageNeighbours runId="R1" onOpen={onOpen} onCompareTo={onCompareTo} load={load} />);
    await user.click(await screen.findByRole('button', { name: 'CHILD' }));
    expect(onOpen).toHaveBeenCalledWith('CHILD');
    expect(screen.getByRole('button', { name: 'UP' }).closest('li')).toHaveTextContent('← from');
    await user.click(screen.getAllByRole('button', { name: 'Compare' })[1]!);
    expect(onCompareTo).toHaveBeenCalledWith(['R1', 'UP']);
  });

  it('explains an empty lineage', async () => {
    render(<LineageNeighbours runId="R1" onOpen={() => {}} load={async () => ({ count: 0, edges: [] })} />);
    expect(await screen.findByText('No lineage edges recorded for this run.')).toBeInTheDocument();
  });

  it('shows a retryable error', async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error('boom')).mockResolvedValue({ count: 0, edges: [] });
    const user = userEvent.setup();
    render(<LineageNeighbours runId="R1" onOpen={() => {}} load={load} />);
    expect(await screen.findByText('Error: boom')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  });
});

describe('RunInspector view switcher', () => {
  it('is a tablist with the explorer views and arrow-key navigation', async () => {
    const user = userEvent.setup();
    const setDetailView = vi.fn();
    render(
      <RunInspector
        selected={run} capsule={capsule} detailError={null} runs={[run]} isDistributed={false}
        detailView="inspect" setDetailView={setDetailView} replayResult={null}
        secretsState={null} childrenState={null} forensicsState={null}
        onSelect={() => {}} onAction={() => {}}
      />,
    );
    const tabs = screen.getByRole('tablist', { name: 'Run detail view' });
    expect(tabs).toHaveTextContent('Integrity');
    expect(tabs).toHaveTextContent('Lineage');
    expect(tabs).not.toHaveTextContent('Children');
    screen.getByRole('tab', { name: 'Inspect' }).focus();
    await user.keyboard('{ArrowRight}');
    expect(setDetailView).toHaveBeenCalledWith('trace');
  });
});
