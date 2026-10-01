/**
 * Runs view: URL state (ADR-0232 D2) + filter bar (D1/D3, ADR-0233, ADR-0234 D2).
 *
 * Acceptance criteria pinned here:
 * - Every Runs view-state key is validated on read; garbage degrades to the default.
 * - Committed changes (push) are history entries, so Back undoes them.
 * - Leaving the Runs tab strips its keys; other keys survive.
 * - Typing `dim:` offers observed values; a truncated list says it is partial.
 * - Enter applies the draft; a parse error is announced (role=alert).
 * - A truncated or incomplete result says so, and shows the equivalent CLI.
 */
import { act, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useUrlState } from '@/lib/useUrlState';
import FilterBar from '@/components/dashboard/tabs/runs/FilterBar';
import { endOfDay } from '@/components/dashboard/tabs/runs/useFilteredRuns';
import {
  activeTerm, completeTerm, parseDate, parseScope, parseSort, parseStatus, parseView,
  RUNS_VIEW_PARAMS, stripRunsViewParams,
} from '@/components/dashboard/tabs/runs/viewState';
import type { FilterRunsResult, FilterSuggestResult } from '@/lib/api';

describe('runs view-state parsers', () => {
  it('accept known values and fall back on anything else', () => {
    expect(parseStatus('error')).toBe('error');
    expect(parseStatus('<script>')).toBe('all');
    expect(parseSort('longest')).toBe('longest');
    expect(parseSort('random')).toBe('newest');
    expect(parseScope('tree')).toBe('tree');
    expect(parseScope('galaxy')).toBe('node');
    expect(parseView('trace')).toBe('trace');
    // `replay` is session-only: a link to it would open on nothing.
    expect(parseView('replay')).toBe('inspect');
    expect(parseDate('2026-09-01')).toBe('2026-09-01');
    expect(parseDate('yesterday')).toBe('');
  });

  it('strips only the Runs keys from a query string', () => {
    const p = new URLSearchParams('tab=runs&f=status:error&scope=tree&run=R1&view=trace&sub=x');
    stripRunsViewParams(p);
    expect(p.toString()).toBe('tab=runs&sub=x');
    expect(RUNS_VIEW_PARAMS).toContain('f');
  });

  it('finds the term being typed and completes it', () => {
    expect(activeTerm('status:error model:gp')).toEqual({ dimension: 'model', partial: 'gp', negate: false });
    expect(activeTerm('-asset:')).toEqual({ dimension: 'asset', partial: '', negate: true });
    expect(activeTerm('status:error ')).toBeNull();
    expect(completeTerm('status:error model:gp', 'gpt-4o')).toBe('status:error model:gpt-4o ');
    expect(completeTerm('asset:', 'my summarizer')).toBe('asset:"my summarizer" ');
  });

  it('makes the until date inclusive', () => {
    expect(endOfDay('2026-09-01')).toBe('2026-09-01T23:59:59Z');
    expect(endOfDay('')).toBeUndefined();
  });
});

describe('useUrlState push option (Back is undo)', () => {
  beforeEach(() => { window.history.replaceState({}, '', '/dashboard?tab=runs'); });

  it('pushes a history entry for committed changes, replaces otherwise', () => {
    const before = window.history.length;
    const { result: pushed } = renderHook(() => useUrlState('f', '', { push: true }));
    act(() => pushed.current[1]('status:error'));
    expect(window.history.length).toBe(before + 1);
    expect(new URLSearchParams(window.location.search).get('f')).toBe('status:error');

    const { result: replaced } = renderHook(() => useUrlState('q', ''));
    act(() => replaced.current[1]('abc'));
    expect(window.history.length).toBe(before + 1);
  });

  it('does not push when the value is unchanged', () => {
    window.history.replaceState({}, '', '/dashboard?f=status%3Aerror');
    const before = window.history.length;
    const { result } = renderHook(() => useUrlState('f', '', { push: true }));
    act(() => result.current[1]('status:error'));
    expect(window.history.length).toBe(before);
  });
});

function result(over: Partial<FilterRunsResult> = {}): FilterRunsResult {
  return {
    filter: 'status:error', scope: 'node', where: 'status = error',
    cli_equivalent: "nova query --select 'count()' --where 'status = error'",
    matched: 1, truncated: false, complete: true, incomplete_reasons: [],
    since: '1970-01-01T00:00:00Z', until: '2026-10-01T00:00:00Z', items: [], ...over,
  };
}

describe('FilterBar', () => {
  const suggest = vi.fn(async (dimension: string): Promise<FilterSuggestResult> => ({
    dimension, values: ['claude-x', 'gpt-4o', 'gpt-4o-mini'], truncated: true,
    since: '', until: '',
  }));

  it('suggests observed values, flags a partial list, and completes on Enter', async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(
      <FilterBar value="" onApply={onApply} scope="node" onScopeChange={() => {}}
        result={null} loading={false} error={null} suggest={suggest} />,
    );
    const box = screen.getByRole('combobox', { name: 'Filter runs' });
    await user.type(box, 'model:gp');
    const options = await screen.findAllByRole('option');
    expect(options.map(o => o.textContent)).toEqual(['gpt-4o', 'gpt-4o-mini']);
    expect(screen.getByText(/Partial list/)).toBeInTheDocument();
    expect(suggest).toHaveBeenCalledWith('model');

    await user.keyboard('{ArrowDown}{Enter}');
    expect(box).toHaveValue('model:gpt-4o ');
    expect(onApply).not.toHaveBeenCalled();

    await user.keyboard('{Enter}');
    expect(onApply).toHaveBeenCalledWith('model:gpt-4o');
  });

  it('announces a parse error', () => {
    render(
      <FilterBar value="cost:>1" onApply={() => {}} scope="node" onScopeChange={() => {}}
        result={null} loading={false} error="'cost' is a metric" suggest={suggest} />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent("'cost' is a metric");
    expect(screen.getByRole('combobox')).toHaveAttribute('aria-invalid', 'true');
  });

  it('states truncation and incompleteness, and shows the CLI equivalent', () => {
    render(
      <FilterBar value="status:error" onApply={() => {}} scope="tree" onScopeChange={() => {}}
        result={result({ scope: 'tree', matched: 300, truncated: true, complete: false,
          incomplete_reasons: ['P1: 3 of 64 children have arrived, so this tree is still filling'] })}
        loading={false} error={null} suggest={suggest} />,
    );
    expect(screen.getByTestId('filter-summary')).toHaveTextContent('0 of 300 whole trees containing a match');
    expect(screen.getByTestId('filter-summary')).toHaveTextContent('truncated');
    expect(screen.getByRole('note')).toHaveTextContent('still filling');
    expect(screen.getByText(/nova query --select/)).toBeInTheDocument();
  });

  it('exposes scope as a radio group and reports changes', async () => {
    const user = userEvent.setup();
    const onScopeChange = vi.fn();
    render(
      <FilterBar value="" onApply={() => {}} scope="node" onScopeChange={onScopeChange}
        result={null} loading={false} error={null} suggest={suggest} />,
    );
    expect(screen.getByRole('radio', { name: 'node' })).toHaveAttribute('aria-checked', 'true');
    await user.click(screen.getByRole('radio', { name: 'tree' }));
    expect(onScopeChange).toHaveBeenCalledWith('tree');
  });
});
