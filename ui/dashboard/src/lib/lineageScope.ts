/**
 * Semantic zoom and scoping for the dashboard lineage view (issue #274, first slice).
 *
 * Pure functions only — no React, no fetch — so the bounds and the scope semantics are
 * unit-testable on their own.
 *
 * Semantics follow the store, not a UI invention:
 * - A node id is `<kind>:<ref>`, as `/api/lineage/edges` returns it.
 * - A *scope* is the neighbourhood `nova lineage export-graph --ref R --kind K --depth D`
 *   exports: provenance (walk source -> target) UNION blast radius (walk target -> source),
 *   each up to D hops, plus the edges whose two endpoints are both in that node set
 *   (`lineage/analytics/_graph.py:collect_subgraph`). It is NOT an undirected BFS: a run that
 *   consumed the same asset as the focus run is not in the focus run's neighbourhood.
 * - Every level is bounded. Nothing here returns more than its budget of nodes; the caller
 *   shows the remainder as a count with a "narrow the scope" control.
 */

/** The fields of a lineage edge this module reads. */
export interface ScopeEdge {
  edge_id: string;
  edge_type: string;
  source: string;
  target: string;
  capsule_run_id?: string;
  confidence?: string;
  created_at?: string;
}

export type ZoomLevel = 'overview' | 'entity' | 'call';

export const ZOOM_LEVELS: { value: ZoomLevel; label: string; help: string }[] = [
  { value: 'overview', label: 'Overview', help: 'One group per node kind (runs, assets, artifacts) with edge counts between groups.' },
  { value: 'entity', label: 'Runs & assets', help: 'Individual runs, assets and artifacts, at most a fixed number at a time.' },
  { value: 'call', label: 'Calls', help: 'The model and tool calls recorded in one run\'s capsule. Needs a run scope.' },
];

/** Depths a scope may take. The CLI accepts more; the dashboard bounds the neighbourhood. */
export const SCOPE_DEPTHS = [1, 2, 3] as const;
export type ScopeDepth = (typeof SCOPE_DEPTHS)[number];
export const DEFAULT_SCOPE_DEPTH: ScopeDepth = 2;

/** Nodes drawn at the entity level before the "N more" control appears. */
export const ENTITY_NODE_BUDGET = 60;
/** Each "show more" press adds this many nodes ... */
export const ENTITY_NODE_STEP = 60;
/** ... up to this hard ceiling. Past it the only way forward is a narrower scope. */
export const ENTITY_NODE_CEILING = 240;
/** Calls listed at the call level before the "N more" control appears. */
export const CALL_BUDGET = 50;
export const CALL_CEILING = 500;

export function parseDepth(raw: string | null | undefined): ScopeDepth {
  const n = Number(raw);
  return (SCOPE_DEPTHS as readonly number[]).includes(n) ? (n as ScopeDepth) : DEFAULT_SCOPE_DEPTH;
}

export function parseZoom(raw: string | null | undefined): ZoomLevel {
  return raw === 'overview' || raw === 'call' ? raw : 'entity';
}

/** Split `<kind>:<ref>` at the first colon. A bare string has kind ''. */
export function splitNodeId(id: string): { kind: string; ref: string } {
  const colon = id.indexOf(':');
  if (colon <= 0) return { kind: '', ref: id };
  return { kind: id.slice(0, colon), ref: id.slice(colon + 1) };
}

/** Every node id that appears as an endpoint, in first-seen order. */
export function nodeIdsOf(edges: ScopeEdge[]): string[] {
  const seen = new Set<string>();
  for (const e of edges) {
    seen.add(e.source);
    seen.add(e.target);
  }
  return [...seen];
}

export function degreeMap(edges: ScopeEdge[]): Map<string, number> {
  const deg = new Map<string, number>();
  for (const e of edges) {
    deg.set(e.source, (deg.get(e.source) ?? 0) + 1);
    deg.set(e.target, (deg.get(e.target) ?? 0) + 1);
  }
  return deg;
}

export interface Neighbourhood {
  /** Node id -> hop distance from the focus (0 for the focus itself). */
  hops: Map<string, number>;
  /** Edges with both endpoints in the neighbourhood (the induced subgraph). */
  edges: ScopeEdge[];
  /** False when the focus is not an endpoint of any edge given. */
  found: boolean;
}

/**
 * Provenance UNION blast radius of `focus` within `depth` hops over `edges`, plus the
 * induced edges. Mirrors `collect_subgraph` over whatever edges the caller has loaded.
 */
export function neighbourhood(edges: ScopeEdge[], focus: string, depth: number): Neighbourhood {
  const out = new Map<string, ScopeEdge[]>();
  const inc = new Map<string, ScopeEdge[]>();
  let found = false;
  for (const e of edges) {
    if (e.source === focus || e.target === focus) found = true;
    (out.get(e.source) ?? out.set(e.source, []).get(e.source)!).push(e);
    (inc.get(e.target) ?? inc.set(e.target, []).get(e.target)!).push(e);
  }
  const hops = new Map<string, number>([[focus, 0]]);
  const walk = (dir: 'out' | 'in') => {
    const seen = new Set<string>([focus]);
    let frontier = [focus];
    for (let hop = 1; hop <= depth && frontier.length > 0; hop++) {
      const next: string[] = [];
      for (const cur of frontier) {
        for (const e of (dir === 'out' ? out.get(cur) : inc.get(cur)) ?? []) {
          const n = dir === 'out' ? e.target : e.source;
          if (seen.has(n)) continue;
          seen.add(n);
          next.push(n);
          const prev = hops.get(n);
          if (prev === undefined || hop < prev) hops.set(n, hop);
        }
      }
      frontier = next;
    }
  };
  walk('out');
  walk('in');
  const induced = edges.filter((e) => hops.has(e.source) && hops.has(e.target));
  return { hops, edges: induced, found };
}

export interface BoundedGraph {
  /** Node ids drawn, in priority order. */
  nodeIds: string[];
  /** Edges whose two endpoints are both drawn. */
  edges: ScopeEdge[];
  /** Distinct nodes in the input. */
  totalNodes: number;
  /** totalNodes - nodeIds.length. */
  hiddenNodes: number;
  /** Edges dropped because an endpoint is not drawn. */
  hiddenEdges: number;
}

/**
 * Keep at most `budget` nodes. Priority: the focus first, then smaller hop distance (when
 * `hops` is given), then higher degree, then node id — deterministic, so the same data
 * always draws the same nodes.
 */
export function boundGraph(
  edges: ScopeEdge[],
  budget: number,
  opts: { focus?: string | null; hops?: Map<string, number> } = {},
): BoundedGraph {
  const ids = nodeIdsOf(edges);
  const deg = degreeMap(edges);
  const { focus, hops } = opts;
  const rank = (id: string) => (id === focus ? -1 : hops?.get(id) ?? 0);
  ids.sort((a, b) => rank(a) - rank(b) || (deg.get(b) ?? 0) - (deg.get(a) ?? 0) || (a < b ? -1 : a > b ? 1 : 0));
  const cap = Math.max(1, Math.floor(budget));
  const kept = ids.slice(0, cap);
  const keep = new Set(kept);
  const drawn = edges.filter((e) => keep.has(e.source) && keep.has(e.target));
  return {
    nodeIds: kept,
    edges: drawn,
    totalNodes: ids.length,
    hiddenNodes: ids.length - kept.length,
    hiddenEdges: edges.length - drawn.length,
  };
}

export interface KindCluster {
  kind: string;
  count: number;
}

export interface ClusterEdge {
  sourceKind: string;
  targetKind: string;
  edgeType: string;
  count: number;
}

/** Overview level: one cluster per node kind, edges aggregated by (kind, edge type, kind). */
export function overview(edges: ScopeEdge[]): { clusters: KindCluster[]; links: ClusterEdge[] } {
  const perKind = new Map<string, Set<string>>();
  const links = new Map<string, ClusterEdge>();
  for (const e of edges) {
    const s = splitNodeId(e.source).kind || 'unknown';
    const t = splitNodeId(e.target).kind || 'unknown';
    (perKind.get(s) ?? perKind.set(s, new Set()).get(s)!).add(e.source);
    (perKind.get(t) ?? perKind.set(t, new Set()).get(t)!).add(e.target);
    const key = `${s}\u0000${e.edge_type}\u0000${t}`;
    const cur = links.get(key);
    if (cur) cur.count += 1;
    else links.set(key, { sourceKind: s, targetKind: t, edgeType: e.edge_type, count: 1 });
  }
  const clusters = [...perKind.entries()]
    .map(([kind, set]) => ({ kind, count: set.size }))
    .sort((a, b) => b.count - a.count || (a.kind < b.kind ? -1 : 1));
  const linkList = [...links.values()].sort(
    (a, b) => b.count - a.count || `${a.sourceKind}${a.edgeType}${a.targetKind}`.localeCompare(`${b.sourceKind}${b.edgeType}${b.targetKind}`),
  );
  return { clusters, links: linkList };
}

/** Quote a CLI argument for a POSIX shell only when it needs it. */
export function shellArg(s: string): string {
  if (s !== '' && /^[A-Za-z0-9_@%+=:,./-]+$/.test(s)) return s;
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

/**
 * The `nova lineage` commands equivalent to the current scope. Flags verified against
 * `nova lineage export-graph|provenance|blast-radius --help`:
 *   export-graph [--ref R] [--kind K] [--depth D]
 *   provenance R [--kind K] [--depth D]      blast-radius R [--kind K] [--depth D]
 * Unscoped, the graph equivalent is the whole-graph export.
 */
export function scopeCliCommands(focus: string | null, depth: number): string[] {
  if (!focus) return ['nova lineage export-graph'];
  const { kind, ref } = splitNodeId(focus);
  const kindFlag = kind ? ` --kind ${shellArg(kind)}` : '';
  const r = shellArg(ref);
  return [
    `nova lineage export-graph --ref ${r}${kindFlag} --depth ${depth}`,
    `nova lineage provenance ${r}${kindFlag} --depth ${depth}`,
    `nova lineage blast-radius ${r}${kindFlag} --depth ${depth}`,
  ];
}

/** One model or tool call at the call level — metadata only, never content. */
export interface CallRow {
  kind: 'model' | 'tool';
  id: string;
  name: string;
  status: string;
  startedAt: string;
  durationMs: number | null;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '';
}

/**
 * Project capsule call records onto display rows. Reads only identifiers, the model or tool
 * name, status, start time and duration: never messages, choices, arguments or results.
 * Sorted by start time (records without one keep their file order, after the timed ones).
 */
export function callRows(
  modelCalls: Array<Record<string, unknown>> | undefined,
  toolCalls: Array<Record<string, unknown>> | undefined,
): CallRow[] {
  const rows: Array<CallRow & { order: number }> = [];
  let order = 0;
  for (const c of modelCalls ?? []) {
    rows.push({
      kind: 'model',
      id: str(c.model_call_id) || `model#${order}`,
      name: str(c['gen_ai.request.model']) || str(c['gen_ai.response.model']) || str(c['gen_ai.system']) || 'model call',
      status: str(c.status),
      startedAt: str(c.started_at),
      durationMs: typeof c.duration_ms === 'number' ? c.duration_ms : null,
      order: order++,
    });
  }
  for (const c of toolCalls ?? []) {
    rows.push({
      kind: 'tool',
      id: str(c.tool_call_id) || `tool#${order}`,
      name: str(c.tool_name) || 'tool call',
      status: str(c.status),
      startedAt: str(c.started_at),
      durationMs: typeof c.duration_ms === 'number' ? c.duration_ms : null,
      order: order++,
    });
  }
  rows.sort((a, b) => {
    if (a.startedAt && b.startedAt) return a.startedAt.localeCompare(b.startedAt) || a.order - b.order;
    if (a.startedAt) return -1;
    if (b.startedAt) return 1;
    return a.order - b.order;
  });
  return rows.map(({ order: _order, ...r }) => r);
}

/**
 * Compare the neighbourhood drawn from the loaded edges with the node set the store's own
 * provenance + blast-radius traversals returned. `storeIds` excludes the focus.
 */
export function compareWithStore(localHops: Map<string, number>, storeIds: Iterable<string>, focus: string) {
  const store = new Set(storeIds);
  store.delete(focus);
  const missing = [...store].filter((id) => !localHops.has(id)).sort();
  return { storeCount: store.size, missing };
}
