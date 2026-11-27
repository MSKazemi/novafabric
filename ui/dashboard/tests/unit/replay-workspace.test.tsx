/**
 * Replay workspace (issue #269) — the mode contract and its side-effect gate.
 *
 * Acceptance criteria pinned here:
 * - Opening the workspace starts no replay: it only reads the replayability report.
 * - Modes that re-run the workload (mocked strict, mocked --permissive, intervention)
 *   are marked side-effecting and have no way to start from the dashboard, even if a
 *   server were to report them startable.
 * - A read-only mode is startable only when the server says so, and only through the
 *   Runs tab's confirm flow (onAction), never directly.
 * - The report shows servable vs not servable calls and the refusal, and an
 *   unavailable report is "unknown", never a fabricated verdict.
 * - Each mode shows the exact `nova replay` command, shell-quoted.
 * - A late response for a previously selected run is never shown.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ReplayWorkspace from '@/components/replay/ReplayWorkspace';
import { servabilityLine } from '@/components/replay/ModeCard';
import {
  REPLAY_MODES,
  mockedDryRunCommand,
  replayCommand,
  shellQuote,
  startableAction,
} from '@/components/replay/modes';
import RunInspector from '@/components/dashboard/tabs/runs/RunInspector';
import { parseView } from '@/components/dashboard/tabs/runs/viewState';
import type { FullCapsule, ReplayabilitySummary, ReplayabilityView, RunSummary } from '@/lib/api';

const run: RunSummary = {
  run_id: 'R1', status: 'success', created_at: '2026-11-27T10:00:00Z', finished_at: null,
  duration_ms: 1000, exit_code: 0, model_call_count: 2, tool_call_count: 0, mutating_tool_count: 0,
  command: ['python', 'agent.py'], novafabric_version: '0.109.0', capsule_path: '/runs/R1',
};

const refusing: ReplayabilitySummary = {
  schema: 'novafabric.replayability/1',
  classifier_version: '0.109.0',
  replayable: 'partial',
  strict_mocked: 'refuse',
  refusal: { type: 'ModelSurfaceNotServable', message: '1 model call not servable' },
  reexecution: { reexecutable: true, source: 'command', guard: null },
  model_calls: { recorded: 2, servable: 1, not_servable: 1, live: 0, covered: 0 },
  tool_calls: { recorded: 0, servable: 0, not_servable: 0 },
  reasons: [{
    code: 'model_surface_not_servable', count: 1, refuses_strict: true,
    summary: '1 model call not servable', detail: 'replay cannot serve mistral',
    by_system: { mistral: { count: 1, hosts: ['api.mistral.ai'] } },
  }],
};

const ALL_STARTABLE = { forensic: true, dry_run: true, semantic: true, exact: true, mocked: true, intervention: true };

function view(overrides: Partial<ReplayabilityView> = {}): ReplayabilityView {
  return {
    run_id: 'R1', current: refusing, current_error: null, recorded: null, recorded_state: 'absent',
    recorded_error: null,
    dashboard_modes: { forensic: true, dry_run: true, semantic: true, exact: true, mocked: false, intervention: false },
    ...overrides,
  };
}

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor);
  else Reflect.deleteProperty(navigator, 'clipboard');
});

describe('mode contracts', () => {
  it('has the five real modes (mocked twice: strict and permissive) and no live mode', () => {
    expect(REPLAY_MODES.map(m => m.cliMode)).toEqual(['forensic', 'semantic', 'exact', 'mocked', 'mocked', 'intervention']);
    expect(REPLAY_MODES.some(m => /live/i.test(m.id))).toBe(false);
  });

  it('marks every mode that re-runs the workload as side-effecting', () => {
    for (const m of REPLAY_MODES) expect(m.sideEffecting).toBe(m.executesWorkload);
  });

  it('never makes a workload-executing mode startable, whatever the server says', () => {
    for (const m of REPLAY_MODES.filter(x => x.executesWorkload)) {
      expect(startableAction(m, ALL_STARTABLE)).toBeNull();
    }
  });

  it('starts a read-only mode only on an explicit server `true`', () => {
    const forensic = REPLAY_MODES.find(m => m.id === 'forensic')!;
    expect(startableAction(forensic, { forensic: true })).toBe('replay');
    expect(startableAction(forensic, { forensic: false })).toBeNull();
    expect(startableAction(forensic, {})).toBeNull();
    expect(startableAction(forensic, null)).toBeNull();
  });

  it('builds the exact command, shell-quoting the capsule path', () => {
    const byId = Object.fromEntries(REPLAY_MODES.map(m => [m.id, m]));
    expect(replayCommand(byId.forensic, '/runs/R1')).toBe('nova replay /runs/R1 --mode forensic');
    expect(replayCommand(byId['mocked-permissive'], '/runs/R1')).toBe('nova replay /runs/R1 --mode mocked --permissive');
    expect(replayCommand(byId.intervention, '/runs/R1'))
      .toBe('nova replay /runs/R1 --mode intervention --intervention-file spec.yaml');
    expect(mockedDryRunCommand("/my runs/it's")).toBe(`nova replay '/my runs/it'\\''s' --dry-run`);
    expect(shellQuote('$(rm -rf ~)')).toBe(`'$(rm -rf ~)'`);
    expect(shellQuote('')).toBe(`''`);
  });

  it('says what the report means for each mode, and unknown when there is none', () => {
    const strict = REPLAY_MODES.find(m => m.id === 'mocked')!;
    const permissive = REPLAY_MODES.find(m => m.id === 'mocked-permissive')!;
    expect(servabilityLine(strict, refusing)).toMatch(/refused before anything runs \(ModelSurfaceNotServable\)/);
    expect(servabilityLine(permissive, refusing)).toMatch(/1 model call replay cannot serve would go LIVE/);
    expect(servabilityLine(strict, null)).toMatch(/^Unknown/);
    const none = { ...refusing, replayable: 'none' as const, reexecution: { reexecutable: false, source: null, guard: null } };
    expect(servabilityLine(permissive, none)).toMatch(/cannot be re-run/);
  });
});

describe('ReplayWorkspace', () => {
  it('only reads on open, and shows the report', async () => {
    const load = vi.fn(async () => view());
    const onAction = vi.fn();
    render(<ReplayWorkspace run={run} capsulePath="/runs/R1" onAction={onAction} load={load} />);
    expect(await screen.findByText('would refuse')).toBeInTheDocument();
    expect(load).toHaveBeenCalledWith('R1');
    expect(onAction).not.toHaveBeenCalled();
    const report = screen.getByRole('region', { name: 'Replayability report' });
    expect(report).toHaveTextContent('ModelSurfaceNotServable');
    expect(report).toHaveTextContent('1 not servable');
    expect(report).toHaveTextContent('mistral: 1 (api.mistral.ai)');
    expect(report).toHaveTextContent('written without replayability.json');
  });

  it('offers no start for side-effecting modes and routes read-only ones through onAction', async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(<ReplayWorkspace run={run} capsulePath="/runs/R1" onAction={onAction} load={async () => view({ dashboard_modes: ALL_STARTABLE })} />);
    await screen.findByText('would refuse');
    for (const title of ['Mocked — strict (default)', 'Mocked — permissive', 'Intervention']) {
      const card = screen.getByRole('article', { name: title });
      expect(card).toHaveTextContent('side-effecting');
      expect(within(card).queryByRole('button', { name: /run .* in the dashboard/i })).toBeNull();
      expect(card).toHaveTextContent('Copy only');
    }
    await user.click(screen.getByRole('button', { name: 'Run Forensic in the dashboard (asks for confirmation)' }));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction).toHaveBeenCalledWith(run, 'replay');
  });

  it('shows the strict mocked check command on the mocked cards', async () => {
    render(<ReplayWorkspace run={run} capsulePath="/runs/R1" onAction={() => {}} load={async () => view()} />);
    await screen.findByText('would refuse');
    const card = screen.getByRole('article', { name: 'Mocked — strict (default)' });
    expect(card).toHaveTextContent('nova replay /runs/R1 --mode mocked');
    expect(card).toHaveTextContent('nova replay /runs/R1 --dry-run');
  });

  it('reports an unavailable report as unknown, keeps the commands, and retries', async () => {
    const user = userEvent.setup();
    const load = vi.fn()
      .mockRejectedValueOnce(new Error('403 Forbidden'))
      .mockResolvedValueOnce(view());
    render(<ReplayWorkspace run={run} capsulePath="/runs/R1" onAction={() => {}} load={load} />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('403 Forbidden');
    expect(alert).toHaveTextContent('unknown');
    expect(screen.getByText('nova replay /runs/R1 --mode forensic')).toBeInTheDocument();
    // No server answer: nothing is startable.
    expect(screen.queryByRole('button', { name: /in the dashboard/i })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('would refuse')).toBeInTheDocument();
  });

  it('shows a classifier error instead of a verdict', async () => {
    render(<ReplayWorkspace run={run} capsulePath="/runs/R1" onAction={() => {}} load={async () => view({ current: null, current_error: 'CapsuleEntryError: model-calls.jsonl is a directory' })} />);
    expect(await screen.findByText(/could not be classified/)).toHaveTextContent('model-calls.jsonl is a directory');
    expect(screen.queryByText('would refuse')).toBeNull();
    expect(screen.queryByText('would start')).toBeNull();
  });

  it('copies a command and announces it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<ReplayWorkspace run={run} capsulePath="/runs/R1" onAction={() => {}} load={async () => view()} />);
    await screen.findByText('would refuse');
    fireEvent.click(screen.getByRole('button', { name: 'Copy: nova replay /runs/R1 --mode forensic' }));
    expect(writeText).toHaveBeenCalledWith('nova replay /runs/R1 --mode forensic');
    expect(await screen.findByText('Copied the Forensic command.')).toBeInTheDocument();
  });

  it("never shows the previous run's late report", async () => {
    let resolveFirst: (v: ReplayabilityView) => void = () => {};
    const load = vi.fn((id: string) => id === 'R1'
      ? new Promise<ReplayabilityView>(r => { resolveFirst = r; })
      : Promise.resolve(view({ run_id: 'R2', current: { ...refusing, strict_mocked: 'start', refusal: null, reasons: [] } })));
    const { rerender } = render(<ReplayWorkspace run={run} capsulePath="/runs/R1" onAction={() => {}} load={load} />);
    const run2 = { ...run, run_id: 'R2', capsule_path: '/runs/R2' };
    rerender(<ReplayWorkspace run={run2} capsulePath="/runs/R2" onAction={() => {}} load={load} />);
    expect(await screen.findByText('would start')).toBeInTheDocument();
    resolveFirst(view());
    await new Promise(r => setTimeout(r, 0));
    expect(screen.queryByText('would refuse')).toBeNull();
  });
});

describe('Runs inspector hook-up', () => {
  it('offers a linkable "Replay modes" view', async () => {
    expect(parseView('replay-modes')).toBe('replay-modes');
    const user = userEvent.setup();
    const setDetailView = vi.fn();
    const capsule = {
      run_id: 'R1', capsule_path: '/runs/R1', manifest: { run_id: 'R1' }, trace: [],
      model_calls: [], tool_calls: [], lineage: [], assets: [], inputs: [], outputs: [],
    } as unknown as FullCapsule;
    render(
      <RunInspector
        selected={run} capsule={capsule} detailError={null} runs={[run]} isDistributed={false}
        detailView="inspect" setDetailView={setDetailView} replayResult={null}
        secretsState={null} childrenState={null} forensicsState={null}
        onSelect={() => {}} onAction={() => {}}
      />,
    );
    await user.click(screen.getByRole('tab', { name: 'Replay modes' }));
    expect(setDetailView).toHaveBeenCalledWith('replay-modes');
  });
});
