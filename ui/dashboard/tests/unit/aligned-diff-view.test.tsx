/**
 * Issue #270 (first slice) — the Diff tab's aligned A/B comparison.
 *
 * Every verdict comes from the server's `/api/diff` response (the engine `nova diff`
 * runs): pairs in alignment order, the first divergence matched to its pair by record
 * id, verdicts in words, and the equivalent CLI command. A changed or partial diff is
 * never shown as identical, and an unreadable first divergence is "not derivable".
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import AlignedDiffView from '@/components/diff/AlignedDiffView';
import {
  buildAlignedComparison,
  describeRow,
  diffCliCommand,
  shellQuote,
} from '@/components/diff/alignedComparison';

const ZERO_SKIPPED = { a: { model_calls: 0, tool_calls: 0 }, b: { model_calls: 0, tool_calls: 0 } };

/** `/api/diff` shape: the engine's report under `report`, canonical fields beside it. */
function apiResponse(overrides: Record<string, unknown> = {}, report: Record<string, unknown> = {}) {
  return {
    run_a_id: 'RUN-A',
    run_b_id: 'RUN-B',
    report: {
      run_a_id: 'RUN-A',
      run_b_id: 'RUN-B',
      env_changes: [],
      model_call_pairs: [
        { changed: false, model_call_id_a: 'MC-1', model_call_id_b: 'MC-1', request_changed: false, response_changed: false, provider_changed: false, params_changed: false, changed_params: [] },
        { changed: true, model_call_id_a: 'MC-2', model_call_id_b: 'MC-2', request_changed: true, response_changed: true, provider_changed: false, params_changed: true, changed_params: ['temperature'] },
        { added: true, model_call_id_b: 'MC-3' },
      ],
      tool_call_pairs: [
        { changed: true, tool_name: 'search', tool_call_id_a: 'TC-1', tool_call_id_b: 'TC-1', arguments_changed: false, result_changed: false, status_changed: true, status_a: 'ok', status_b: 'error' },
        { removed: true, tool_name: 'fetch', tool_call_id_a: 'TC-2' },
      ],
      output_changes: [],
      skipped_malformed_lines: ZERO_SKIPPED,
      first_divergence: {
        index: 2, index_a: 2, index_b: 2, stream: 'model_calls', event_type: 'model_call',
        reason: 'changed', aspects: ['request', 'response', 'params'],
        record_id_a: 'MC-2', record_id_b: 'MC-2', timeline: 'started_at',
      },
      ...report,
    },
    summary: { changed: 2, added: 1, removed: 1 },
    has_changes: true,
    skipped_malformed_lines: ZERO_SKIPPED,
    ...overrides,
  };
}

const IDENTICAL = apiResponse(
  { summary: { changed: 0, added: 0, removed: 0 }, has_changes: false },
  {
    model_call_pairs: [{ changed: false, model_call_id_a: 'MC-1', model_call_id_b: 'MC-1' }],
    tool_call_pairs: [],
    first_divergence: null,
  },
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('buildAlignedComparison', () => {
  it('keeps the server alignment order and verdicts, and marks the first divergent pair by record id', () => {
    const c = buildAlignedComparison(apiResponse());
    expect(c.rows.model_calls.map(r => r.verdict)).toEqual(['unchanged', 'changed', 'added']);
    expect(c.rows.tool_calls.map(r => r.verdict)).toEqual(['changed', 'removed']);
    expect(c.firstDivergence.kind).toBe('found');
    if (c.firstDivergence.kind !== 'found') return;
    expect(c.firstDivergence.rowKey).toBe('model_calls-1');
    expect(c.rows.model_calls.filter(r => r.isFirstDivergence).map(r => r.idA)).toEqual(['MC-2']);
    expect(c.hasChanges).toBe(true);
    expect(c.runA).toBe('RUN-A');
  });

  it('matches an added first divergence to the pair present only in run B', () => {
    const c = buildAlignedComparison(apiResponse({}, {
      first_divergence: {
        index: 4, index_a: null, index_b: 4, stream: 'model_calls', event_type: 'model_call',
        reason: 'added', aspects: [], record_id_a: null, record_id_b: 'MC-3', timeline: 'per_stream',
      },
    }));
    expect(c.firstDivergence.kind === 'found' && c.firstDivergence.rowKey).toBe('model_calls-2');
  });

  it('does not guess a pair when the divergence carries no record id', () => {
    const c = buildAlignedComparison(apiResponse({}, {
      first_divergence: {
        index: 0, index_a: 0, index_b: 0, stream: 'tool_calls', event_type: 'tool_call',
        reason: 'changed', aspects: ['status'], record_id_a: null, record_id_b: null, timeline: 'per_stream',
      },
    }));
    expect(c.firstDivergence.kind === 'found' && c.firstDivergence.rowKey).toBeNull();
    expect([...c.rows.model_calls, ...c.rows.tool_calls].some(r => r.isFirstDivergence)).toBe(false);
  });

  it('reports "not derivable" when the server sends no first_divergence key', () => {
    const resp = apiResponse();
    const { first_divergence: _fd, ...report } = resp.report;
    const c = buildAlignedComparison({ ...resp, report });
    expect(c.firstDivergence.kind).toBe('not_derivable');
    expect(c.hasChanges).toBe(true);
  });

  it('reports "not derivable" for a null divergence that contradicts a listed change', () => {
    const c = buildAlignedComparison(apiResponse({}, { first_divergence: null }));
    expect(c.firstDivergence.kind).toBe('not_derivable');
  });

  it('reports "not derivable" for a malformed divergence', () => {
    const c = buildAlignedComparison(apiResponse({}, { first_divergence: { index: 'two', stream: 'nope' } }));
    expect(c.firstDivergence.kind).toBe('not_derivable');
  });

  it('never calls a diff identical when a pair differs, even if has_changes is missing or false', () => {
    const c = buildAlignedComparison(apiResponse({ has_changes: false }));
    expect(c.hasChanges).toBe(true);
    const { has_changes: _h, ...noVerdict } = apiResponse();
    expect(buildAlignedComparison(noVerdict).hasChanges).toBe(true);
  });

  it('counts an environment- or output-only difference as a change with no call divergence', () => {
    const c = buildAlignedComparison(apiResponse({ has_changes: true }, {
      model_call_pairs: [], tool_call_pairs: [], first_divergence: null,
      output_changes: [{ path: 'stdout.txt', before_hash: 'a', after_hash: 'b' }],
    }));
    expect(c.firstDivergence.kind).toBe('none');
    expect(c.hasChanges).toBe(true);
    expect(c.outputChanges).toBe(1);
  });

  it('counts skipped malformed record lines from either run', () => {
    const c = buildAlignedComparison(apiResponse({
      skipped_malformed_lines: { a: { model_calls: 0, tool_calls: 2 }, b: { model_calls: 1, tool_calls: 0 } },
    }));
    expect(c.skippedLines).toBe(3);
  });

  it('reads the flat `nova diff --output-format json` report too', () => {
    const resp = apiResponse();
    const c = buildAlignedComparison({ ...resp.report, has_changes: true });
    expect(c.rows.model_calls).toHaveLength(3);
    expect(c.firstDivergence.kind).toBe('found');
  });

  it('survives a non-object response without inventing pairs', () => {
    const c = buildAlignedComparison(null);
    expect(c.rows.model_calls).toEqual([]);
    expect(c.firstDivergence.kind).toBe('not_derivable');
  });

  it('describes what differs in words', () => {
    const c = buildAlignedComparison(apiResponse());
    expect(describeRow(c.rows.model_calls[1])).toBe('request, response, params (temperature)');
    expect(describeRow(c.rows.tool_calls[0])).toBe('status ok → error');
    expect(describeRow(c.rows.model_calls[2])).toBe('only in run B');
    expect(describeRow(c.rows.tool_calls[1])).toBe('only in run A');
  });
});

describe('diffCliCommand', () => {
  it('builds the nova diff invocation with run ids as positional refs', () => {
    expect(diffCliCommand('01KZA', '01KZB')).toBe('nova diff 01KZA 01KZB');
    expect(diffCliCommand('01KZA', '01KZB', { json: true })).toBe('nova diff 01KZA 01KZB --output-format json');
  });

  it('shell-quotes a ref that is not shell-safe', () => {
    expect(shellQuote("run a'1")).toBe(`'run a'\\''1'`);
    expect(diffCliCommand('a b', 'c')).toBe("nova diff 'a b' c");
  });
});

describe('<AlignedDiffView>', () => {
  it('labels every pair in words and marks the first divergent row', () => {
    render(<AlignedDiffView raw={apiResponse()} />);
    const models = screen.getByRole('table', { name: /Model calls/ });
    const rows = within(models).getAllByRole('row').slice(1);
    expect(rows.map(r => r.getAttribute('data-verdict'))).toEqual(['unchanged', 'changed', 'added']);
    expect(within(rows[0]).getByText('Unchanged')).toBeInTheDocument();
    expect(within(rows[2]).getByText('Added in B')).toBeInTheDocument();
    expect(rows[1]).toHaveAttribute('aria-current', 'step');
    expect(within(rows[1]).getByText('First divergence')).toBeInTheDocument();
    const tools = screen.getByRole('table', { name: /Tool calls/ });
    expect(within(tools).getByText('Removed in B')).toBeInTheDocument();
    expect(screen.getByText('Step 2: model call changed (request, response, params)')).toBeInTheDocument();
  });

  it('scrolls the first divergent row into view on load and focuses it on request', () => {
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
    try {
      render(<AlignedDiffView raw={apiResponse()} />);
      expect(scroll).toHaveBeenCalled();
      const target = screen.getAllByRole('row').find(r => r.getAttribute('aria-current') === 'step')!;
      expect(document.activeElement).not.toBe(target);
      fireEvent.click(screen.getByRole('button', { name: 'Go to first divergence' }));
      expect(document.activeElement).toBe(target);
    } finally {
      Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
    }
  });

  it('hides unchanged pairs on request but keeps the counts', () => {
    render(<AlignedDiffView raw={apiResponse()} />);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Show only differences' }));
    const models = screen.getByRole('table', { name: /Model calls/ });
    expect(within(models).queryByText('Unchanged')).toBeNull();
    expect(models).toHaveAccessibleName(/1 unchanged/);
  });

  it('says "no differences" only when the server found none', () => {
    render(<AlignedDiffView raw={IDENTICAL} />);
    expect(screen.getByText(/No differences: the server found no changed/)).toBeInTheDocument();
    expect(screen.getByText(/None in model or tool calls/)).toBeInTheDocument();
  });

  it('flags an incomplete comparison instead of calling it identical', () => {
    render(<AlignedDiffView raw={{ ...IDENTICAL, skipped_malformed_lines: { a: { model_calls: 1, tool_calls: 0 }, b: { model_calls: 0, tool_calls: 0 } } }} />);
    expect(screen.getByText(/Comparison incomplete: 1 record line could not be read/)).toBeInTheDocument();
    expect(screen.queryByText(/No differences: the server found/)).toBeNull();
  });

  it('shows "not derivable" when the server reports no first divergence field', () => {
    const resp = apiResponse();
    const { first_divergence: _fd, ...report } = resp.report;
    render(<AlignedDiffView raw={{ ...resp, report }} />);
    expect(screen.getByText(/Not derivable\./)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Go to first divergence' })).toBeNull();
  });

  it('copies the equivalent nova diff command and announces it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const descriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    try {
      render(<AlignedDiffView raw={apiResponse()} />);
      expect(screen.getByText('nova diff RUN-A RUN-B')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Copy CLI command: nova diff RUN-A RUN-B' }));
      await waitFor(() => expect(screen.getByText('Command copied.')).toBeInTheDocument());
      expect(writeText).toHaveBeenCalledWith('nova diff RUN-A RUN-B');
    } finally {
      if (descriptor) Object.defineProperty(navigator, 'clipboard', descriptor);
      else Reflect.deleteProperty(navigator, 'clipboard');
    }
  });

  it('never renders prompt, response or tool content', () => {
    const secret = 'sk-live-do-not-show';
    const resp = apiResponse({}, {
      model_call_pairs: [{ changed: true, response_changed: true, model_call_id_a: 'MC-1', model_call_id_b: 'MC-1', 'gen_ai.request.messages': [{ content: secret }] }],
    });
    const { container } = render(<AlignedDiffView raw={resp} />);
    expect(container.textContent).not.toContain(secret);
  });
});
