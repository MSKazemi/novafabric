/**
 * Lineage explorer: semantic zoom (overview -> runs & assets -> calls), a scoped neighbourhood
 * view (depth 1-3) entered from a run or asset, and a table view of the same nodes and edges.
 *
 * Every level draws a bounded number of nodes; the rest are counted, never silently dropped.
 * The scope, depth, level and view live in the URL (`lineage_*`), so a scoped view is
 * shareable and the browser Back button undoes a scope change.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../../../../lib/api';
import type { FullCapsule } from '../../../../lib/api';
import type { AssetRecord, LineageEdgeRecord } from '../../../../lib/fixtures';
import type { LineageNode } from '../../../../lib/lineage';
import { useUrlState } from '../../../../lib/useUrlState';
import {
  CALL_BUDGET,
  CALL_CEILING,
  ENTITY_NODE_BUDGET,
  ENTITY_NODE_CEILING,
  ENTITY_NODE_STEP,
  SCOPE_DEPTHS,
  ZOOM_LEVELS,
  boundGraph,
  callRows,
  compareWithStore,
  degreeMap,
  neighbourhood,
  nodeIdsOf,
  overview,
  parseDepth,
  parseZoom,
  scopeCliCommands,
  splitNodeId,
  type ZoomLevel,
} from '../../../../lib/lineageScope';
import LineageGraph from '../../../lineage/LineageGraph';
import CopyButton from '../../../ui/CopyButton';
import { EdgeTable, NodeTable, type TableNode } from './LineageTable';
import LineageOverviewGraph, { plural } from './LineageOverviewGraph';

type ViewMode = 'graph' | 'table';

const PILL = 'px-2.5 py-1 text-[11px] rounded border transition-colors disabled:opacity-40 disabled:cursor-not-allowed focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]';
const PILL_ON = 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_15%,transparent)] text-[var(--color-accent)]';
const PILL_OFF = 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)]';
const NOTICE = 'rounded border px-3 py-2 text-xs';
const NOTICE_INFO = `${NOTICE} border-[var(--color-border)] bg-[var(--color-bg-raised)] text-[var(--color-text-muted)]`;
const NOTICE_WARN = `${NOTICE} border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] text-[var(--color-text)]`;

export interface LineageExplorerProps {
  /** Edges after the tab's search / edge-type filters. */
  edges: LineageEdgeRecord[];
  assets: AssetRecord[];
  /** Edges the tab loaded from the server (before filters). */
  loadedCount: number;
  /** Edges in the store, when the server reported it. */
  storeTotal?: number;
  /** True when the store holds older edges than the loaded page. */
  truncated: boolean;
  /** True when a search or edge-type filter is narrowing `edges`. */
  filtered: boolean;
  onNodeSelect?: (node: LineageNode | null) => void;
}

/** "N more — narrow the scope" control shared by the bounded levels. */
function MoreControl({
  hidden,
  noun,
  canShowMore,
  step,
  onShowMore,
  hint,
}: {
  hidden: number;
  noun: string;
  canShowMore: boolean;
  step: number;
  onShowMore: () => void;
  hint: string;
}) {
  if (hidden <= 0) return null;
  return (
    <div role="status" className={`${NOTICE_WARN} flex flex-wrap items-center gap-2`}>
      <span>
        <strong>{hidden} more {noun}{hidden === 1 ? '' : 's'}</strong> not drawn — narrow the scope. {hint}
      </span>
      {canShowMore && (
        <button type="button" onClick={onShowMore} className={`${PILL} ${PILL_OFF}`}>
          Show {Math.min(step, hidden)} more
        </button>
      )}
    </div>
  );
}

function CliBlock({ commands, note }: { commands: string[]; note?: string }) {
  return (
    <div className="space-y-1">
      <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
      {commands.map((c) => (
        <div key={c} className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 pr-20">
          <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all">$ {c}</pre>
          <div className="absolute top-1.5 right-1.5"><CopyButton text={c} label="CLI" /></div>
        </div>
      ))}
      {note && <p className="text-[10px] text-[var(--color-text-faint)]">{note}</p>}
    </div>
  );
}

type StoreCheck =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'done'; storeCount: number; missing: string[] }
  | { state: 'error'; message: string };

/** Ask the store's own provenance + blast-radius traversals for the scope's node set. */
function useStoreCheck(focus: string | null, depth: number, local: Map<string, number> | null) {
  const [check, setCheck] = useState<StoreCheck>({ state: 'idle' });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!focus || !local) { setCheck({ state: 'idle' }); return; }
    let cancelled = false;
    setCheck({ state: 'loading' });
    const { ref } = splitNodeId(focus);
    Promise.all([api.provenance(ref, depth), api.blastRadius(ref, depth)])
      .then(([p, b]) => {
        if (cancelled) return;
        const ids = [...p.ancestors, ...b.descendants].map((r) => `${r.kind}:${r.ref}`);
        setCheck({ state: 'done', ...compareWithStore(local, ids, focus) });
      })
      .catch((e: unknown) => {
        if (!cancelled) setCheck({ state: 'error', message: e instanceof Error ? e.message : String(e) });
      });
    return () => { cancelled = true; };
    // `local` changes identity whenever the loaded edges change; that is when to re-check.
  }, [focus, depth, local, nonce]);
  return { check, retry: () => setNonce((n) => n + 1) };
}

type CallsState =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'done'; capsule: FullCapsule }
  | { state: 'error'; message: string };

function useRunCalls(runId: string | null) {
  const [calls, setCalls] = useState<CallsState>({ state: 'idle' });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!runId) { setCalls({ state: 'idle' }); return; }
    let cancelled = false;
    setCalls({ state: 'loading' });
    api.getRun(runId)
      .then((capsule) => { if (!cancelled) setCalls({ state: 'done', capsule }); })
      .catch((e: unknown) => {
        if (!cancelled) setCalls({ state: 'error', message: e instanceof Error ? e.message : String(e) });
      });
    return () => { cancelled = true; };
  }, [runId, nonce]);
  return { calls, retry: () => setNonce((n) => n + 1) };
}

export default function LineageExplorer({
  edges,
  assets,
  loadedCount,
  storeTotal,
  truncated,
  filtered,
  onNodeSelect,
}: LineageExplorerProps) {
  const [focusRaw, setFocus] = useUrlState('lineage_focus', '', { push: true });
  const [depthRaw, setDepthRaw] = useUrlState('lineage_depth', '2', { push: true });
  const [zoomRaw, setZoomRaw] = useUrlState('lineage_zoom', 'entity', { push: true });
  const [viewRaw, setViewRaw] = useUrlState('lineage_view', 'graph', { push: true });
  const [kindFilter, setKindFilter] = useUrlState('lineage_kind', '', { push: true });
  const [selected, setSelected] = useState<string | null>(null);
  const [nodeBudget, setNodeBudget] = useState(ENTITY_NODE_BUDGET);
  const [callBudget, setCallBudget] = useState(CALL_BUDGET);

  const focus = focusRaw || null;
  const depth = parseDepth(depthRaw);
  const view: ViewMode = viewRaw === 'table' ? 'table' : 'graph';
  const focusKind = focus ? splitNodeId(focus).kind : '';
  const runScope = focus && focusKind === 'run' ? splitNodeId(focus).ref : null;
  const requestedZoom = parseZoom(zoomRaw);
  // The call level needs a run to read calls from; anything else falls back to entities.
  const zoom: ZoomLevel = requestedZoom === 'call' && !runScope ? 'entity' : requestedZoom;

  // A new scope or level starts from the default budget again.
  useEffect(() => { setNodeBudget(ENTITY_NODE_BUDGET); setCallBudget(CALL_BUDGET); }, [focus, depth, zoom, kindFilter]);

  const kindEdges = useMemo(
    () => (kindFilter
      ? edges.filter((e) => splitNodeId(e.source).kind === kindFilter || splitNodeId(e.target).kind === kindFilter)
      : edges),
    [edges, kindFilter],
  );

  const scope = useMemo(() => (focus ? neighbourhood(kindEdges, focus, depth) : null), [kindEdges, focus, depth]);
  const scopedEdges = scope ? scope.edges : kindEdges;
  const bounded = useMemo(
    () => boundGraph(scopedEdges, nodeBudget, { focus, hops: scope?.hops }),
    [scopedEdges, nodeBudget, focus, scope],
  );
  const boundedEdges = useMemo(() => bounded.edges as LineageEdgeRecord[], [bounded]);
  const ov = useMemo(() => overview(scopedEdges), [scopedEdges]);
  const { check, retry: retryCheck } = useStoreCheck(focus, depth, scope?.hops ?? null);
  const { calls, retry: retryCalls } = useRunCalls(zoom === 'call' ? runScope : null);

  const tableNodes: TableNode[] = useMemo(() => {
    const deg = degreeMap(bounded.edges);
    return bounded.nodeIds.map((id) => ({ id, hop: scope?.hops.get(id), degree: deg.get(id) ?? 0 }));
  }, [bounded, scope]);

  const enterScope = useCallback((id: string) => {
    setFocus(id);
    setSelected(id);
    if (requestedZoom === 'overview') setZoomRaw('entity');
  }, [setFocus, setZoomRaw, requestedZoom]);

  const handleGraphSelect = useCallback((n: LineageNode | null) => {
    setSelected(n?.id ?? null);
    onNodeSelect?.(n);
  }, [onNodeSelect]);

  const cli = scopeCliCommands(focus, depth);
  const graphNodeCount = nodeIdsOf(bounded.edges).length;
  const hiddenForView = view === 'graph' ? bounded.totalNodes - graphNodeCount : bounded.hiddenNodes;

  return (
    <section aria-label="Lineage explorer" className="space-y-3">
      {/* Controls */}
      <div role="toolbar" aria-label="Lineage view controls" className="flex flex-wrap items-center gap-3">
        <div role="group" aria-label="Zoom level" className="flex items-center gap-1">
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mr-1">Level</span>
          {ZOOM_LEVELS.map((z) => {
            const disabled = z.value === 'call' && !runScope;
            return (
              <button
                key={z.value}
                type="button"
                aria-pressed={zoom === z.value}
                disabled={disabled}
                title={disabled ? 'Scope to a run first: calls are read from one run\'s capsule.' : z.help}
                onClick={() => setZoomRaw(z.value)}
                className={`${PILL} ${zoom === z.value ? PILL_ON : PILL_OFF}`}
              >
                {z.label}
              </button>
            );
          })}
        </div>
        <div role="group" aria-label="Presentation" className="flex items-center gap-1">
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mr-1">View</span>
          {(['graph', 'table'] as ViewMode[]).map((v) => {
            const disabled = v === 'graph' && zoom === 'call';
            return (
              <button
                key={v}
                type="button"
                aria-pressed={view === v || (zoom === 'call' && v === 'table')}
                disabled={disabled}
                title={disabled ? 'The calls of one run are a flat list; they have a table view only.' : undefined}
                onClick={() => setViewRaw(v)}
                className={`${PILL} ${view === v || (zoom === 'call' && v === 'table') ? PILL_ON : PILL_OFF}`}
              >
                {v === 'graph' ? 'Graph' : 'Table'}
              </button>
            );
          })}
        </div>
        <div role="group" aria-label="Scope" className="flex flex-wrap items-center gap-2">
          {focus ? (
            <>
              <span className="text-xs text-[var(--color-text-muted)]">
                Scope: <span className="font-mono text-[var(--color-text)] break-all">{focus}</span>
              </span>
              <label className="flex items-center gap-1 text-xs text-[var(--color-text-muted)]">
                Depth
                <select
                  aria-label="Neighbourhood depth in hops"
                  value={depth}
                  onChange={(e) => setDepthRaw(e.target.value)}
                  className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-1 font-mono text-xs"
                >
                  {SCOPE_DEPTHS.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </label>
              {selected && selected !== focus && (
                <button
                  type="button"
                  onClick={() => enterScope(selected)}
                  title={`Show only the neighbourhood of ${selected}`}
                  className={`${PILL} ${PILL_OFF}`}
                >
                  Scope to selected node
                </button>
              )}
              <button type="button" onClick={() => setFocus('')} className={`${PILL} ${PILL_OFF}`}>
                Clear scope
              </button>
            </>
          ) : (
            <button
              type="button"
              disabled={!selected}
              onClick={() => selected && enterScope(selected)}
              title={selected ? `Show only the neighbourhood of ${selected}` : 'Select a run or asset in the graph or table first.'}
              className={`${PILL} ${PILL_OFF}`}
            >
              {selected ? 'Scope to selected node' : 'Select a node to scope'}
            </button>
          )}
          {kindFilter && (
            <button type="button" onClick={() => setKindFilter('')} className={`${PILL} ${PILL_ON}`}>
              Only edges touching {kindFilter} nodes <span aria-hidden="true">×</span><span className="sr-only">(remove)</span>
            </button>
          )}
        </div>
      </div>

      <CliBlock
        commands={cli}
        note={focus
          ? 'export-graph (experimental) exports this neighbourhood: provenance plus blast radius to the chosen depth. The two queries list its nodes. The dashboard draws the same set from the loaded edges.'
          : 'Unscoped, the equivalent is the whole-graph export. Pick a node and scope to get the neighbourhood commands.'}
      />

      {/* Source and completeness */}
      <p className="text-[11px] text-[var(--color-text-faint)]">
        Source: the local lineage store via <code className="font-mono">/api/lineage/edges</code>, newest {loadedCount} edge{loadedCount === 1 ? '' : 's'}
        {storeTotal !== undefined ? ` of ${storeTotal} in the store` : ''}
        {filtered ? '; search or edge-type filter applied' : ''}.
      </p>
      {truncated && (
        <p role="status" className={NOTICE_WARN}>
          The store holds older edges that were not loaded. Neighbourhoods and counts here cover the loaded edges only;
          raise <strong>Show</strong> above or use the CLI command for the whole store.
        </p>
      )}
      {focus && scope && !scope.found && (
        <p role="status" className={NOTICE_WARN}>
          <span className="font-mono">{focus}</span> is not an endpoint of any loaded edge{filtered || kindFilter ? ' that passes the filters' : ''}.
        </p>
      )}
      {focus && check.state === 'loading' && <p role="status" className={NOTICE_INFO}>Checking this neighbourhood against the store…</p>}
      {focus && check.state === 'error' && (
        <div role="alert" className={`${NOTICE_WARN} flex flex-wrap items-center gap-2`}>
          <span>Could not check the neighbourhood against the store: {check.message}</span>
          <button type="button" onClick={retryCheck} className={`${PILL} ${PILL_OFF}`}>Retry</button>
        </div>
      )}
      {focus && check.state === 'done' && (
        <p role="status" className={check.missing.length > 0 ? NOTICE_WARN : NOTICE_INFO}>
          The store&apos;s provenance and blast-radius queries return {plural('node', check.storeCount)} within {depth} hop{depth === 1 ? '' : 's'} (all edge types).
          {check.missing.length > 0
            ? ` ${check.missing.length} of them ${check.missing.length === 1 ? 'is' : 'are'} not in the loaded${filtered || kindFilter ? ', filtered' : ''} edges and not drawn: ${check.missing.slice(0, 5).join(', ')}${check.missing.length > 5 ? ', …' : ''}.`
            : ' Every one of them is in the loaded edges.'}
        </p>
      )}

      {/* Levels */}
      {zoom === 'overview' && (
        <div className="space-y-3">
          <p className="text-xs text-[var(--color-text-muted)]">
            Overview: {plural('node kind', ov.clusters.length)}, {plural('relationship type', ov.links.length)}
            {focus ? ' in this neighbourhood' : ''}. Choose a kind to see its runs, assets or artifacts.
          </p>
          {view === 'graph' && <LineageOverviewGraph clusters={ov.clusters} links={ov.links} onPick={(k) => { setKindFilter(k); setZoomRaw('entity'); }} />}
          <div role="group" aria-label="Zoom into a node kind" className="flex flex-wrap gap-2">
            {ov.clusters.map((c) => (
              <button
                key={c.kind}
                type="button"
                onClick={() => { setKindFilter(c.kind); setZoomRaw('entity'); }}
                className={`${PILL} ${PILL_OFF}`}
              >
                Show {plural(c.kind, c.count)}
              </button>
            ))}
          </div>
          {view === 'table' && (
            <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-x-auto">
              <table className="w-full text-xs">
                <caption className="text-left px-3 py-2 text-[11px] text-[var(--color-text-muted)]">
                  Relationships between node kinds, with how many edges of each type were recorded. Associations, not causes.
                </caption>
                <thead>
                  <tr>
                    <th scope="col" className="text-left px-2 py-1.5 text-[10px] uppercase text-[var(--color-text-faint)]">Source kind</th>
                    <th scope="col" className="text-left px-2 py-1.5 text-[10px] uppercase text-[var(--color-text-faint)]">Relationship</th>
                    <th scope="col" className="text-left px-2 py-1.5 text-[10px] uppercase text-[var(--color-text-faint)]">Target kind</th>
                    <th scope="col" className="text-left px-2 py-1.5 text-[10px] uppercase text-[var(--color-text-faint)]">Edges</th>
                  </tr>
                </thead>
                <tbody>
                  {ov.links.map((l) => (
                    <tr key={`${l.sourceKind}|${l.edgeType}|${l.targetKind}`} className="border-t border-[var(--color-border)]">
                      <td className="px-2 py-1.5 font-mono">{l.sourceKind}</td>
                      <td className="px-2 py-1.5 font-mono text-[var(--color-text-muted)]">{l.edgeType}</td>
                      <td className="px-2 py-1.5 font-mono">{l.targetKind}</td>
                      <td className="px-2 py-1.5 font-mono">{l.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {zoom === 'entity' && (
        <div className="space-y-3">
          <MoreControl
            hidden={hiddenForView}
            noun="node"
            canShowMore={nodeBudget < ENTITY_NODE_CEILING}
            step={ENTITY_NODE_STEP}
            onShowMore={() => setNodeBudget((b) => Math.min(ENTITY_NODE_CEILING, b + ENTITY_NODE_STEP))}
            hint={focus
              ? 'Lower the depth, filter by edge type or search above.'
              : 'Select a run or asset and scope to it, pick a kind in the overview, or filter above.'}
          />
          {view === 'graph' ? (
            <LineageGraph
              key={`${focus ?? 'all'}:${depth}`}
              edges={boundedEdges}
              assets={assets}
              initialSelectedNodeId={focus}
              initialMode="all"
              emptyMessage={focus
                ? 'No loaded edges in this neighbourhood.'
                : 'No lineage edges yet. Run captures that consume registered assets to build the graph.'}
              onNodeSelect={handleGraphSelect}
            />
          ) : (
            <div className="space-y-3">
              <NodeTable
                nodes={tableNodes}
                focus={focus}
                selected={selected}
                onSelect={setSelected}
                onScope={enterScope}
              />
              <EdgeTable edges={bounded.edges} />
            </div>
          )}
        </div>
      )}

      {zoom === 'call' && runScope && (
        <CallLevel runId={runScope} state={calls} retry={retryCalls} budget={callBudget} onShowMore={() => setCallBudget((b) => Math.min(CALL_CEILING, b + CALL_BUDGET))} />
      )}
    </section>
  );
}

function CallLevel({
  runId,
  state,
  retry,
  budget,
  onShowMore,
}: {
  runId: string;
  state: CallsState;
  retry: () => void;
  budget: number;
  onShowMore: () => void;
}) {
  const rows = useMemo(
    () => (state.state === 'done' ? callRows(state.capsule.model_calls, state.capsule.tool_calls) : []),
    [state],
  );
  if (state.state === 'loading' || state.state === 'idle') return <p role="status" className={NOTICE_INFO}>Loading the calls of run {runId}…</p>;
  if (state.state === 'error') {
    return (
      <div role="alert" className={`${NOTICE_WARN} flex flex-wrap items-center gap-2`}>
        <span>Could not read the calls of run {runId}: {state.message}</span>
        <button type="button" onClick={retry} className={`${PILL} ${PILL_OFF}`}>Retry</button>
      </div>
    );
  }
  const shown = rows.slice(0, budget);
  const unavailable = state.capsule.capsule_available === false;
  return (
    <div className="space-y-3">
      <p className={NOTICE_INFO}>
        Calls are read from this run&apos;s capsule (<code className="font-mono">model-calls.jsonl</code>,
        {' '}<code className="font-mono">tool-calls.jsonl</code>), not from lineage edges: a call belongs to the run,
        it is not a lineage relationship. Only ids, names, status and timing are shown here; never prompts, responses,
        arguments or results.
      </p>
      {unavailable && (
        <p role="status" className={NOTICE_WARN}>The capsule directory for this run is not on disk; only its indexed metadata is available, so no calls can be listed.</p>
      )}
      <MoreControl
        hidden={rows.length - shown.length}
        noun="call"
        canShowMore={budget < CALL_CEILING}
        step={CALL_BUDGET}
        onShowMore={onShowMore}
        hint="Open the run in the Runs tab for its full trace."
      />
      {rows.length === 0 && !unavailable ? (
        <p className={NOTICE_INFO}>This run&apos;s capsule records no model or tool calls.</p>
      ) : (
        <CallTable rows={shown} total={rows.length} />
      )}
    </div>
  );
}

function CallTable({ rows, total }: { rows: ReturnType<typeof callRows>; total: number }) {
  const TH = 'text-left font-medium text-[var(--color-text-faint)] uppercase tracking-wider text-[10px] px-2 py-1.5';
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-x-auto">
      <table className="w-full text-xs">
        <caption className="text-left px-3 py-2 text-[11px] text-[var(--color-text-muted)]">
          <span className="font-semibold text-[var(--color-text)]">Calls</span> ({rows.length} of {total}), ordered by start time.
        </caption>
        <thead>
          <tr>
            <th scope="col" className={TH}>Type</th>
            <th scope="col" className={TH}>Model or tool</th>
            <th scope="col" className={TH}>Status</th>
            <th scope="col" className={TH}>Started</th>
            <th scope="col" className={TH}>Duration (ms)</th>
            <th scope="col" className={TH}>Call id</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.kind}:${r.id}`} className="border-t border-[var(--color-border)]">
              <td className="px-2 py-1.5 font-mono">{r.kind}</td>
              <td className="px-2 py-1.5 font-mono text-[var(--color-text)] break-all">{r.name}</td>
              <td className="px-2 py-1.5 font-mono">{r.status || '—'}</td>
              <td className="px-2 py-1.5 font-mono text-[var(--color-text-faint)]">{r.startedAt || '—'}</td>
              <td className="px-2 py-1.5 font-mono">{r.durationMs ?? '—'}</td>
              <td className="px-2 py-1.5 font-mono text-[var(--color-text-faint)] break-all">{r.id}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
