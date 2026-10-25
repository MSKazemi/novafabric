/**
 * Review frontend-dashboard-15 (and the cycle half of -17) — the trace diff read `parent`,
 * but capsule spans carry `parent_span_id`, so every span was a root, the tree was one
 * node, and two structurally different traces rendered as identical.
 */
import { describe, expect, it } from 'vitest';

import { diffSpans, flattenSpans } from '@/components/diff/TraceDiffGraph';

const trace = (extra: Array<Record<string, unknown>> = []) => [
  { span_id: 'r', name: 'agent', duration_ms: 100 },
  { span_id: 'm', parent_span_id: 'r', name: 'model', duration_ms: 60 },
  ...extra,
];

describe('flattenSpans', () => {
  it('builds the tree from parent_span_id', () => {
    const flat = flattenSpans(trace());
    expect(flat.map((s) => [s.path, s.depth])).toEqual([['agent', 0], ['agent/model', 1]]);
  });

  it('still accepts the older `parent` spelling', () => {
    const flat = flattenSpans([
      { span_id: 'r', name: 'agent' },
      { span_id: 'c', parent: 'r', name: 'tool' },
    ]);
    expect(flat.map((s) => s.path)).toEqual(['agent', 'agent/tool']);
  });

  it('stops on a parent cycle instead of recursing without end', () => {
    const flat = flattenSpans([
      { span_id: 'r', name: 'agent' },
      { span_id: 'a', parent_span_id: 'b', name: 'a' },
      { span_id: 'b', parent_span_id: 'a', name: 'b' },
    ]);
    expect(flat.map((s) => s.path)).toEqual(['agent']);
  });
});

describe('diffSpans', () => {
  it('reports a child span one trace has and the other lacks', () => {
    const a = flattenSpans(trace());
    const b = flattenSpans(trace([{ span_id: 't', parent_span_id: 'r', name: 'tool', duration_ms: 5 }]));
    const kinds = diffSpans(a, b).map((d) => [d.name, d.kind]);
    expect(kinds).toContainEqual(['tool', 'added']);
  });
});
