/**
 * Review frontend-dashboard-02 — the Diff tab called two runs "identical" when their
 * model calls changed, were added or were removed.
 *
 * The server's `model_call_pairs` carry `changed` / `added` / `removed` /
 * `request_changed` / `response_changed` booleans, never a `changes` array, so the
 * adapter counted 0 changed model calls and hard-coded 0 added/removed. It now takes
 * the counts from the server's canonical `summary` / `sections` (the verdict
 * `nova diff --assert-no-regressions` uses) and lists each pair's changed parts.
 */
import { describe, expect, it } from 'vitest';

import { adaptDiffReport } from '@/components/dashboard/tabs/DiffTab';

const response = {
  run_a_id: 'A',
  run_b_id: 'B',
  report: {
    run_a_id: 'A',
    run_b_id: 'B',
    env_changes: [],
    model_call_pairs: [
      { changed: true, request_changed: false, response_changed: true, provider_changed: false,
        params_changed: false, changed_params: [], model_call_id_a: 'a1', model_call_id_b: 'b1' },
      { added: true, model_call_id_b: 'b2' },
    ],
    tool_call_pairs: [],
    output_changes: [],
  },
  summary: { changed: 1, added: 1, removed: 0 },
  sections: {
    model_calls: { aligned: 1, changed: 1, added: 1, removed: 0 },
  },
  has_changes: true,
};

describe('adaptDiffReport', () => {
  it('counts changed and added model calls from the server, never 0', () => {
    const d = adaptDiffReport(response);
    expect(d.summary).toEqual({ changed: 1, added: 1, removed: 0 });
    expect(d.sections.model_calls.changed).toBe(1);
    expect(d.sections.model_calls.added).toBe(1);
  });

  it('lists what changed in each model-call pair', () => {
    const d = adaptDiffReport(response);
    const [changed, added] = d.sections.model_calls.pairs;
    expect(changed.changes.map(c => c.field)).toEqual(['response']);
    expect(changed.a_call_id).toBe('a1');
    expect(added.changes.map(c => c.field)).toEqual(['call added']);
    expect(added.b_call_id).toBe('b2');
  });

  it('still adapts an older response without the canonical fields', () => {
    const { summary: _s, sections: _x, has_changes: _h, ...legacy } = response;
    const d = adaptDiffReport(legacy);
    expect(d.summary.changed).toBe(1);
    expect(d.summary.added).toBe(1);
  });
});
