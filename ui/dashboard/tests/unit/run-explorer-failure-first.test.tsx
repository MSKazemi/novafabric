/**
 * Issue #268, first slice — failure-first Run Explorer and provenance-aware
 * event inspector.
 *
 * Acceptance criteria pinned here:
 * - Events from model-calls, transport attempts and tool-calls form one list in
 *   recorded `started_at` order; records without a start time go last and the
 *   gap is stated.
 * - Provenance comes from real record fields: transport (`record_role`), replay
 *   placeholder (`replay_placeholder`), replay record (`replay_of_run_id`),
 *   otherwise recorded. A `live` placeholder's outcome is unknown, not failed.
 * - The first observed failure is the earliest non-success event; a retried
 *   transport attempt under a logical call is not it.
 * - `[REDACTED:<rule>]` markers are reported per event.
 * - The inspector selects the first failure by default, moves with ↑/↓, j/k,
 *   Home/End, keeps j/k from reaching the Runs view, and says when a linked
 *   event is not in the capsule rather than showing another.
 * - A failed run's inspector opens with its recorded reason; "Inspect event"
 *   opens the Events view on the first failing event.
 * - The failed-runs strip asks the server for status failure and error with the
 *   list's search and dates, and shows the server's approximate totals.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { FullCapsule, RunSummary } from '@/lib/api';
import {
  buildRunEvents, findRedactions, firstObservedFailure, isFailedRunStatus, runFailure,
} from '@/components/dashboard/tabs/runs/eventModel';
import EventInspector, { EVENT_PAGE } from '@/components/dashboard/tabs/runs/EventInspector';
import { FailedRunsStrip, RunFailureBanner } from '@/components/dashboard/tabs/runs/FailurePanels';
import RunInspector from '@/components/dashboard/tabs/runs/RunInspector';
import { LINKABLE_VIEWS, RUNS_VIEW_PARAMS, parseView } from '@/components/dashboard/tabs/runs/viewState';

const T = (ms: number) => new Date(Date.UTC(2026, 10, 27, 10, 0, 0) + ms).toISOString();

function makeCapsule(over: Record<string, unknown> = {}, manifest: Record<string, unknown> = {}): FullCapsule {
  return {
    run_id: 'R1', capsule_path: '/c/R1',
    manifest: { run_id: 'R1', tool_call_count: 2, ...manifest },
    trace: [], lineage: [], assets: [], inputs: [], outputs: [],
    model_calls: [
      { model_call_id: 'M1', started_at: T(0), duration_ms: 50, status: 'success',
        'gen_ai.request.model': 'gpt-x', tool_choice_call_ids: ['T1'] },
    ],
    transport_model_calls: [
      { model_call_id: 'W1', started_at: T(10), status: 'error', endpoint: 'https://api.example/v1',
        extensions: { 'io.novafabric.record_role': 'transport', 'io.novafabric.logical_call_id': 'M1' } },
    ],
    tool_calls: [
      { tool_call_id: 'T1', started_at: T(1000), status: 'success', tool_name: 'read_file',
        agent_call_id: 'M1', arguments: { token: '[REDACTED:github_pat]', nested: ['[REDACTED:aws_key:sha256:ab12cd34]'] } },
      { tool_call_id: 'T2', started_at: T(2000), status: 'timeout', tool_name: 'send_email',
        error: { type: 'TimeoutError', message: 'upstream timed out' }, log_level: 'error', log_level_source: 'adapter' },
    ],
    ...over,
  } as unknown as FullCapsule;
}

const failedRun: RunSummary = {
  run_id: 'R1', status: 'failure', created_at: T(0), finished_at: T(3000), duration_ms: 3000,
  exit_code: 1, model_call_count: 1, tool_call_count: 2, mutating_tool_count: 0,
  command: ['python', 'agent.py'], novafabric_version: '0.110.0', capsule_path: '/c/R1',
};

describe('event model', () => {
  it('orders every stream by recorded started_at and labels provenance from record fields', () => {
    const m = buildRunEvents(makeCapsule());
    expect(m.events.map(e => e.key)).toEqual(['model:M1', 'transport:W1', 'tool:T1', 'tool:T2']);
    expect(m.events.map(e => e.provenance)).toEqual(['recorded', 'transport', 'recorded', 'recorded']);
    expect(m.events[1].logicalCallId).toBe('M1');
    expect(m.events[3].error).toEqual({ type: 'TimeoutError', message: 'upstream timed out' });
    expect(m.gaps).toEqual([]);
    expect(m.replay).toBeNull();
  });

  it('lists records without a start time last and says so', () => {
    const c = makeCapsule({ tool_calls: [{ tool_call_id: 'U', tool_name: 'x', status: 'success' }] }, { tool_call_count: 1 });
    const m = buildRunEvents(c);
    expect(m.events.at(-1)?.key).toBe('tool:U');
    expect(m.untimed).toBe(1);
    expect(m.gaps.join(' ')).toContain('1 event has no recorded start time');
  });

  it('states a tool-call count that disagrees with the manifest', () => {
    const m = buildRunEvents(makeCapsule({}, { tool_call_count: 5 }));
    expect(m.gaps).toContain('The manifest declares 5 tool calls; tool-calls.jsonl holds 2.');
  });

  it('marks replay records and placeholders; a live placeholder has no outcome', () => {
    const c = makeCapsule({
      model_calls: [], transport_model_calls: [],
      tool_calls: [
        { tool_call_id: 'A', started_at: T(0), status: 'success', tool_name: 'served' },
        { tool_call_id: 'B', started_at: T(5), tool_name: 'ran_live',
          extensions: { 'io.novafabric.replay_placeholder': 'live' } },
      ],
    }, { replay_of_run_id: 'ORIG', replay_mode: 'mocked' });
    const m = buildRunEvents(c);
    expect(m.replay).toEqual({ of: 'ORIG', mode: 'mocked' });
    expect(m.events.map(e => [e.provenance, e.outcome])).toEqual([
      ['replay-record', 'ok'],
      ['replay-placeholder', 'unknown'],
    ]);
    expect(m.gaps.join(' ')).toContain('ran live during the replay; the outcome was not observed');
    expect(firstObservedFailure(m.events)).toBeNull();
  });

  it('never gives two events the same key', () => {
    const c = makeCapsule({ tool_calls: [
      { tool_call_id: 'D', started_at: T(0), status: 'success', tool_name: 'a' },
      { tool_call_id: 'D', started_at: T(1), status: 'success', tool_name: 'b' },
    ] });
    const keys = buildRunEvents(c).events.map(e => e.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('says no events can be listed when the capsule files are missing', () => {
    const c = makeCapsule({ capsule_available: false, model_calls: [], transport_model_calls: [], tool_calls: [] });
    expect(buildRunEvents(c).gaps[0]).toContain('not on disk');
  });

  it('picks the earliest failure, skipping a transport attempt of a logical call', () => {
    const m = buildRunEvents(makeCapsule());
    expect(firstObservedFailure(m.events)?.key).toBe('tool:T2');
  });

  it('finds masking markers anywhere in a record', () => {
    expect(findRedactions({ a: '[REDACTED:github_pat] and [REDACTED:email]', b: [{ c: '[REDACTED:aws_key:sha256:ab12cd34]' }] }))
      .toEqual(['aws_key', 'email', 'github_pat']);
    expect(findRedactions({ a: 'nothing here', n: 3, z: null })).toEqual([]);
  });

  it('reads the failure reason from the recorded status, exit code and manifest error', () => {
    expect(runFailure({ ...failedRun, status: 'success', exit_code: 0 }, {})).toBeNull();
    expect(runFailure(failedRun, { error: { type: 'RuntimeError', message: 'boom' } }))
      .toEqual({ status: 'failure', exitCode: 1, error: { type: 'RuntimeError', message: 'boom' } });
    expect(runFailure({ ...failedRun, status: 'success', exit_code: 3 }, {})?.exitCode).toBe(3);
    expect(['failure', 'error', 'aborted', 'partial'].every(isFailedRunStatus)).toBe(true);
    expect(isFailedRunStatus('running')).toBe(false);
  });
});

function optionText(o: HTMLElement): string { return o.textContent ?? ''; }
function selectedOption(): HTMLElement {
  return screen.getAllByRole('option').find(o => o.getAttribute('aria-selected') === 'true')!;
}

describe('EventInspector', () => {
  it('opens on the first observed failure with recorded and derived fields marked', () => {
    render(<EventInspector capsule={makeCapsule()} />);
    expect(optionText(selectedOption())).toContain('send_email');
    expect(optionText(selectedOption())).toContain('first failure');
    const detail = screen.getByRole('region', { name: 'Event detail' });
    expect(detail).toHaveTextContent('upstream timed out');
    expect(detail).toHaveTextContent('error (set by adapter)');
    expect(within(detail).getByText('Status').parentElement).toHaveTextContent('recorded');
    expect(within(detail).getByText('Outcome').parentElement).toHaveTextContent('derived');
    expect(screen.getByTestId('event-counts')).toHaveTextContent('4 events: 1 model call, 1 HTTP attempt, 2 tool calls · 2 failed');
  });

  it('moves with arrows, j/k and Home/End, and keeps j/k out of the Runs view', async () => {
    const user = userEvent.setup();
    render(<EventInspector capsule={makeCapsule()} />);
    const list = screen.getByRole('listbox', { name: 'Run events in recorded order' });
    list.focus();
    await user.keyboard('{ArrowUp}');
    expect(optionText(selectedOption())).toContain('read_file');
    await user.keyboard('k');
    expect(optionText(selectedOption())).toContain('api.example');
    await user.keyboard('{Home}');
    expect(optionText(selectedOption())).toContain('gpt-x');
    await user.keyboard('{End}');
    expect(optionText(selectedOption())).toContain('send_email');
    expect(list).toHaveAttribute('aria-activedescendant', selectedOption().id);

    const runsViewKeys = vi.fn();
    window.addEventListener('keydown', runsViewKeys);
    fireEvent.keyDown(list, { key: 'k' });
    window.removeEventListener('keydown', runsViewKeys);
    expect(runsViewKeys).not.toHaveBeenCalled();
    expect(optionText(selectedOption())).toContain('read_file');
  });

  it('reports the selection through onSelectKey when controlled', async () => {
    const user = userEvent.setup();
    const onSelectKey = vi.fn();
    render(<EventInspector capsule={makeCapsule()} selectedKey="tool:T1" onSelectKey={onSelectKey} />);
    expect(optionText(selectedOption())).toContain('read_file');
    screen.getByRole('listbox').focus();
    await user.keyboard('j');
    expect(onSelectKey).toHaveBeenCalledWith('tool:T2');
  });

  it('shows masked values, related calls and transport attempts', async () => {
    const user = userEvent.setup();
    render(<EventInspector capsule={makeCapsule()} />);
    await user.click(screen.getAllByRole('option').find(o => optionText(o).includes('read_file'))!);
    const detail = screen.getByRole('region', { name: 'Event detail' });
    expect(detail).toHaveTextContent('Masked at capture by rules aws_key, github_pat');
    await user.click(within(detail).getByRole('button', { name: 'model call gpt-x (M1)' }));
    expect(optionText(selectedOption())).toContain('gpt-x');
    expect(screen.getByRole('region', { name: 'Event detail' })).toHaveTextContent('tool call read_file (T1)');

    await user.click(screen.getAllByRole('option').find(o => optionText(o).includes('api.example'))!);
    const transport = screen.getByRole('region', { name: 'Event detail' });
    expect(transport).toHaveTextContent('Transport attempt');
    expect(within(transport).getByRole('button', { name: 'model call gpt-x (M1)' })).toBeInTheDocument();
  });

  it('jumps to the first failure and filters to failed events', async () => {
    const user = userEvent.setup();
    render(<EventInspector capsule={makeCapsule()} selectedKey={undefined} />);
    await user.click(screen.getAllByRole('option')[0]);
    await user.click(screen.getByRole('button', { name: 'Jump to first failure' }));
    expect(optionText(selectedOption())).toContain('send_email');
    await user.click(screen.getByRole('checkbox', { name: 'Failed events only' }));
    // The retried HTTP attempt failed too: it is listed, but it is not the first failure.
    expect(screen.getAllByRole('option').map(optionText).map(t => t.includes('send_email') || t.includes('api.example')))
      .toEqual([true, true]);
  });

  it('says a linked event is not in the capsule instead of showing another', () => {
    render(<EventInspector capsule={makeCapsule()} selectedKey="tool:NOPE" onSelectKey={() => {}} />);
    expect(screen.getByTestId('event-missing')).toHaveTextContent('tool:NOPE');
    expect(screen.queryByRole('region', { name: 'Event detail' })).toBeNull();
    expect(screen.getAllByRole('option').some(o => o.getAttribute('aria-selected') === 'true')).toBe(false);
  });

  it('renders a long trace a page at a time and still shows a linked event past the page', async () => {
    const tool_calls = Array.from({ length: 450 }, (_, i) => ({
      tool_call_id: `T${i}`, started_at: T(i * 10), status: 'success', tool_name: `tool_${i}`,
    }));
    const c = makeCapsule({ model_calls: [], transport_model_calls: [], tool_calls }, { tool_call_count: 450 });
    const { unmount } = render(<EventInspector capsule={c} />);
    expect(screen.getAllByRole('option')).toHaveLength(EVENT_PAGE);
    expect(screen.getByTestId('event-window')).toHaveTextContent(`Showing ${EVENT_PAGE} of 450 events`);
    await userEvent.setup().click(screen.getByRole('button', { name: `Show ${EVENT_PAGE} more` }));
    expect(screen.getAllByRole('option')).toHaveLength(EVENT_PAGE * 2);
    unmount();

    render(<EventInspector capsule={c} selectedKey="tool:T300" onSelectKey={() => {}} />);
    expect(optionText(selectedOption())).toContain('tool_300');
    expect(selectedOption()).toHaveAttribute('aria-posinset', '301');
    expect(selectedOption()).toHaveAttribute('aria-setsize', '450');
  });

  it('explains a run with no recorded calls', () => {
    render(<EventInspector capsule={makeCapsule({ model_calls: [], transport_model_calls: [], tool_calls: [] }, { tool_call_count: 0 })} />);
    expect(screen.getByText('No model calls, HTTP attempts or tool calls were recorded for this run.')).toBeInTheDocument();
  });
});

describe('RunFailureBanner', () => {
  it('shows the recorded reason and opens the first failing event', async () => {
    const user = userEvent.setup();
    const onInspectEvent = vi.fn();
    const c = makeCapsule({}, { error: { type: 'RuntimeError', message: 'agent crashed' } });
    render(<RunFailureBanner run={failedRun} capsule={c} onInspectEvent={onInspectEvent} />);
    const banner = screen.getByRole('region', { name: 'Failure reason' });
    expect(banner).toHaveTextContent('status failure · exit code 1');
    expect(banner).toHaveTextContent('Recorded error: RuntimeError: agent crashed');
    expect(banner).toHaveTextContent('First observed failing event: tool call send_email (status timeout) — upstream timed out');
    await user.click(within(banner).getByRole('button', { name: 'Inspect event' }));
    expect(onInspectEvent).toHaveBeenCalledWith('tool:T2');
  });

  it('says when no error detail or failing call was recorded, and is absent for a success', () => {
    const c = makeCapsule({ transport_model_calls: [], tool_calls: [] }, { tool_call_count: 0 });
    const { rerender } = render(<RunFailureBanner run={failedRun} capsule={c} />);
    expect(screen.getByTestId('run-failure-banner')).toHaveTextContent('The manifest records no error detail');
    expect(screen.getByTestId('run-failure-banner')).toHaveTextContent('No recorded model or tool call has a non-success status.');
    rerender(<RunFailureBanner run={{ ...failedRun, status: 'success', exit_code: 0 }} capsule={c} />);
    expect(screen.queryByTestId('run-failure-banner')).toBeNull();
  });
});

describe('FailedRunsStrip', () => {
  const summary = (run_id: string, status: string, created_at: string): RunSummary => ({
    ...failedRun, run_id, status, created_at,
  });

  it('asks the server for failure and error runs in the same window and lists them newest first', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const searchRuns = vi.fn(async (q: { status?: string }) => (q.status === 'failure'
      ? { items: [summary('F1', 'failure', T(0))], next_cursor: null, total_approx: 12 }
      : { items: [summary('E1', 'error', T(60_000))], next_cursor: null, total_approx: 1 }));
    render(
      <FailedRunsStrip search=" agent " since="2026-11-01" until="2026-11-27" refreshTick={0}
        selectedId="F1" onOpen={onOpen} searchRuns={searchRuns as never} />,
    );
    const strip = screen.getByRole('region', { name: 'Failed runs first' });
    expect(await within(strip).findByTestId('failed-strip-totals')).toHaveTextContent('about 12 failure · about 1 error');
    expect(searchRuns).toHaveBeenCalledWith(expect.objectContaining({
      status: 'failure', q: 'agent', since: '2026-11-01', until: '2026-11-27T23:59:59.999999', limit: 5,
    }));
    expect(searchRuns).toHaveBeenCalledWith(expect.objectContaining({ status: 'error' }));
    const rows = within(strip).getAllByRole('button');
    expect(rows.map(b => b.textContent)).toEqual([expect.stringContaining('E1'), expect.stringContaining('F1')]);
    expect(rows[1]).toHaveAttribute('aria-current', 'true');
    expect(within(strip).getByTestId('failed-strip-more')).toBeInTheDocument();
    await user.click(rows[1]);
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ run_id: 'F1' }));
  });

  it('says there are none, and shows an error when the server fails', async () => {
    const empty = vi.fn(async () => ({ items: [], next_cursor: null, total_approx: 0 }));
    const { unmount } = render(
      <FailedRunsStrip search="" since="" until="" refreshTick={0} selectedId={null} onOpen={() => {}} searchRuns={empty as never} />,
    );
    expect(await screen.findByText('No run with status failure or error in this search and date window.')).toBeInTheDocument();
    unmount();
    const failing = vi.fn(async () => { throw new Error('server unreachable'); });
    render(
      <FailedRunsStrip search="" since="" until="" refreshTick={0} selectedId={null} onOpen={() => {}} searchRuns={failing as never} />,
    );
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not load failed runs: server unreachable'));
  });
});

describe('Runs inspector hook-up', () => {
  it('offers a linkable Events view and the event in the URL state', () => {
    expect(parseView('events')).toBe('events');
    expect(LINKABLE_VIEWS).toContain('events');
    expect(RUNS_VIEW_PARAMS).toContain('event');
  });

  it("opens a failed run's first failing event from the banner", async () => {
    const user = userEvent.setup();
    const setDetailView = vi.fn();
    const onSelectEvent = vi.fn();
    render(
      <RunInspector
        selected={failedRun} capsule={makeCapsule()} detailError={null} runs={[failedRun]} isDistributed={false}
        detailView="events" setDetailView={setDetailView} replayResult={null}
        secretsState={null} childrenState={null} forensicsState={null}
        onSelect={() => {}} onAction={() => {}}
        selectedEventKey="" onSelectEvent={onSelectEvent}
      />,
    );
    expect(screen.getByRole('tab', { name: 'Events' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Run events' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Inspect event' }));
    expect(onSelectEvent).toHaveBeenCalledWith('tool:T2');
    expect(setDetailView).toHaveBeenCalledWith('events');
    await user.click(screen.getByRole('button', { name: 'verify it in Integrity' }));
    expect(setDetailView).toHaveBeenCalledWith('integrity');
  });
});
