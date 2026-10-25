/** Review frontend-dashboard-17 — malformed traces must not overflow the stack or throw RangeError. */
import { describe, expect, it } from 'vitest';

import { buildSpanAgentMap, minOf } from '@/lib/traceUtils';

describe('buildSpanAgentMap', () => {
  it('terminates on a parent cycle', () => {
    const map = buildSpanAgentMap([
      { span_id: 'r', name: 'root' },
      { span_id: 'a', parent_span_id: 'b', name: 'a' },
      { span_id: 'b', parent_span_id: 'a', name: 'b' },
    ]);
    expect(map.has('a') && map.has('b')).toBe(true);
  });

  it('resolves a 100 000-deep chain without recursion', () => {
    const spans = [{ span_id: 's0', name: 'root' }, { span_id: 's1', parent_span_id: 's0', name: 'agent' }];
    for (let i = 2; i < 100_000; i++) spans.push({ span_id: `s${i}`, parent_span_id: `s${i - 1}`, name: `n${i}` });
    const map = buildSpanAgentMap(spans);
    expect(map.get('s99999')).toBe('agent');
  });

  it('still prefers nova.agent.id and the nearest non-root ancestor', () => {
    const map = buildSpanAgentMap([
      { span_id: 'r', name: 'root' },
      { span_id: 'p', parent_span_id: 'r', name: 'planner' },
      { span_id: 'c', parent_span_id: 'p', name: 'call' },
      { span_id: 'x', parent_span_id: 'p', name: 'tagged', attributes: { 'nova.agent.id': 'agent-7' } },
    ]);
    expect(map.get('c')).toBe('planner');
    expect(map.get('x')).toBe('agent-7');
  });
});

describe('minOf', () => {
  it('does not throw on a very large array and is 0 when empty', () => {
    expect(minOf(Array.from({ length: 500_000 }, (_, i) => 500_000 - i))).toBe(1);
    expect(minOf([])).toBe(0);
  });
});
