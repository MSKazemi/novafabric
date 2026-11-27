/**
 * Semantic zoom / scope helpers for the lineage view (#274). The scope must mean what
 * `nova lineage export-graph --ref R --depth D` means (provenance UNION blast radius, induced
 * edges), and every level must stay inside its node budget.
 */
import { describe, expect, it } from 'vitest';
import {
  boundGraph,
  callRows,
  compareWithStore,
  neighbourhood,
  overview,
  parseDepth,
  parseZoom,
  scopeCliCommands,
  shellArg,
  splitNodeId,
  type ScopeEdge,
} from '@/lib/lineageScope';

const e = (id: string, source: string, edge_type: string, target: string): ScopeEdge => ({ edge_id: id, source, edge_type, target });

// r1 consumed a@1 and m@1; r2 also consumed a@1 (a sibling of r1 through a@1);
// r3 replayed r1; f1 was produced by r1.
const EDGES: ScopeEdge[] = [
  e('e1', 'run:r1', 'consumed', 'asset:a@1'),
  e('e2', 'run:r1', 'consumed', 'asset:m@1'),
  e('e3', 'run:r2', 'consumed', 'asset:a@1'),
  e('e4', 'run:r3', 'replayed_from', 'run:r1'),
  e('e5', 'artifact:f1', 'produced_by', 'run:r1'),
  e('e6', 'run:r4', 'replayed_from', 'run:r3'),
];

describe('neighbourhood', () => {
  it('is provenance plus blast radius, not an undirected walk', () => {
    const n = neighbourhood(EDGES, 'run:r1', 2);
    expect([...n.hops.keys()].sort()).toEqual(
      ['artifact:f1', 'asset:a@1', 'asset:m@1', 'run:r1', 'run:r3', 'run:r4'].sort(),
    );
    // r2 only shares an asset with r1: reaching it needs a forward then a backward step.
    expect(n.hops.has('run:r2')).toBe(false);
    expect(n.found).toBe(true);
  });

  it('respects the depth bound and records hop distance', () => {
    const n = neighbourhood(EDGES, 'run:r1', 1);
    expect(n.hops.get('run:r1')).toBe(0);
    expect(n.hops.get('run:r3')).toBe(1);
    expect(n.hops.has('run:r4')).toBe(false);
  });

  it('keeps only edges with both endpoints in the neighbourhood', () => {
    const n = neighbourhood(EDGES, 'run:r1', 1);
    expect(n.edges.map((x) => x.edge_id).sort()).toEqual(['e1', 'e2', 'e4', 'e5']);
  });

  it('reports a focus that is in no edge', () => {
    const n = neighbourhood(EDGES, 'run:missing', 3);
    expect(n.found).toBe(false);
    expect(n.edges).toEqual([]);
    expect([...n.hops.keys()]).toEqual(['run:missing']);
  });

  it('terminates on cycles', () => {
    const cyc = [e('c1', 'run:a', 'replayed_from', 'run:b'), e('c2', 'run:b', 'replayed_from', 'run:a')];
    expect(neighbourhood(cyc, 'run:a', 3).hops.size).toBe(2);
  });
});

describe('boundGraph', () => {
  const star: ScopeEdge[] = Array.from({ length: 500 }, (_, i) => e(`s${i}`, `run:r${i}`, 'consumed', 'asset:hub@1'));

  it('never keeps more nodes than the budget and counts the rest', () => {
    const b = boundGraph(star, 60);
    expect(b.nodeIds).toHaveLength(60);
    expect(b.totalNodes).toBe(501);
    expect(b.hiddenNodes).toBe(441);
    expect(b.edges.every((x) => b.nodeIds.includes(x.source) && b.nodeIds.includes(x.target))).toBe(true);
  });

  it('puts the highest-degree node first and is deterministic', () => {
    const a = boundGraph(star, 10);
    const b = boundGraph([...star].reverse(), 10);
    expect(a.nodeIds[0]).toBe('asset:hub@1');
    expect(a.nodeIds).toEqual(b.nodeIds);
  });

  it('keeps the focus and then the nearest hops when scoped', () => {
    const n = neighbourhood(EDGES, 'run:r1', 2);
    const b = boundGraph(n.edges, 2, { focus: 'run:r1', hops: n.hops });
    expect(b.nodeIds[0]).toBe('run:r1');
    expect(n.hops.get(b.nodeIds[1])).toBe(1);
  });

  it('treats a non-positive budget as one node, never zero or negative', () => {
    expect(boundGraph(star, 0).nodeIds).toHaveLength(1);
  });
});

describe('overview', () => {
  it('groups nodes by kind and counts edges per kind pair and type', () => {
    const o = overview(EDGES);
    expect(o.clusters).toEqual([
      { kind: 'run', count: 4 },
      { kind: 'asset', count: 2 },
      { kind: 'artifact', count: 1 },
    ]);
    expect(o.links).toContainEqual({ sourceKind: 'run', targetKind: 'asset', edgeType: 'consumed', count: 3 });
    expect(o.links).toContainEqual({ sourceKind: 'run', targetKind: 'run', edgeType: 'replayed_from', count: 2 });
  });
});

describe('scopeCliCommands', () => {
  it('maps an unscoped view to the whole-graph export', () => {
    expect(scopeCliCommands(null, 2)).toEqual(['nova lineage export-graph']);
  });

  it('uses the verified export-graph / provenance / blast-radius flags', () => {
    expect(scopeCliCommands('asset:my-model@v1', 3)).toEqual([
      'nova lineage export-graph --ref my-model@v1 --kind asset --depth 3',
      'nova lineage provenance my-model@v1 --kind asset --depth 3',
      'nova lineage blast-radius my-model@v1 --kind asset --depth 3',
    ]);
  });

  it('quotes a ref the shell would split or expand', () => {
    expect(scopeCliCommands("artifact:out dir/it's.txt", 1)[0]).toBe(
      "nova lineage export-graph --ref 'out dir/it'\\''s.txt' --kind artifact --depth 1",
    );
    expect(shellArg('$(rm -rf ~)')).toBe("'$(rm -rf ~)'");
  });
});

describe('callRows', () => {
  it('keeps metadata only and orders by start time', () => {
    const rows = callRows(
      [{
        model_call_id: 'm1', 'gen_ai.request.model': 'gpt-x', status: 'ok', started_at: '2026-01-01T00:00:02Z', duration_ms: 12,
        'gen_ai.request.messages': [{ role: 'user', content: 'SECRET PROMPT' }],
        'gen_ai.response.choices': [{ message: { content: 'SECRET ANSWER' } }],
      }],
      [{ tool_call_id: 't1', tool_name: 'search', status: 'ok', started_at: '2026-01-01T00:00:01Z', arguments: { q: 'SECRET ARG' }, result: 'SECRET RESULT' }],
    );
    expect(rows.map((r) => r.id)).toEqual(['t1', 'm1']);
    expect(JSON.stringify(rows)).not.toMatch(/SECRET/);
    expect(rows[1]).toEqual({ kind: 'model', id: 'm1', name: 'gpt-x', status: 'ok', startedAt: '2026-01-01T00:00:02Z', durationMs: 12 });
  });

  it('tolerates missing and malformed records', () => {
    const rows = callRows([{}, { duration_ms: 'x' }], undefined);
    expect(rows).toHaveLength(2);
    expect(rows[0].durationMs).toBeNull();
  });
});

describe('small parsers', () => {
  it('bounds depth to 1-3 and defaults to 2', () => {
    expect(parseDepth('1')).toBe(1);
    expect(parseDepth('3')).toBe(3);
    expect(parseDepth('4')).toBe(2);
    expect(parseDepth('nope')).toBe(2);
    expect(parseDepth(null)).toBe(2);
  });

  it('defaults an unknown zoom level to the entity level', () => {
    expect(parseZoom('overview')).toBe('overview');
    expect(parseZoom('call')).toBe('call');
    expect(parseZoom('fleet')).toBe('entity');
  });

  it('splits node ids at the first colon only', () => {
    expect(splitNodeId('artifact:file:a/b')).toEqual({ kind: 'artifact', ref: 'file:a/b' });
    expect(splitNodeId('bare')).toEqual({ kind: '', ref: 'bare' });
  });

  it('reports store nodes the loaded edges do not contain', () => {
    const local = new Map([['run:r1', 0], ['asset:a@1', 1]]);
    expect(compareWithStore(local, ['asset:a@1', 'asset:z@9', 'run:r1'], 'run:r1')).toEqual({ storeCount: 2, missing: ['asset:z@9'] });
  });
});
