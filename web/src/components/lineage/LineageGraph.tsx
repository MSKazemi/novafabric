import { useEffect, useMemo, useState, useRef } from 'react';
import EmptyState from '../ui/EmptyState';
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  MarkerType,
  ReactFlowProvider,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { clsx } from 'clsx';

import {
  buildGraph,
  layoutGraph,
  highlightSubgraph,
  EDGE_TYPE_COLOR,
  EDGE_TYPE_LABEL,
  type LineageEdge,
  type LineageNode,
  type LineageNodeData,
  type ToolbarMode,
} from '../../lib/lineage';
import { lineageEdges as fixtureEdges, registry as fixtureRegistry } from '../../lib/fixtures';
import type { AssetRecord, LineageEdgeRecord } from '../../lib/fixtures';
import LineageNodeRenderer from './LineageNode';

const nodeTypes = { lineage: LineageNodeRenderer };

interface LineageGraphProps {
  /** Lineage edges to render. Falls back to the showcase fixture when absent. */
  edges?: LineageEdgeRecord[];
  /** Asset records used to enrich asset nodes (status, type). Falls back to fixture. */
  assets?: AssetRecord[];
  /** Initial selected node id. Defaults to a fixture asset for the showcase; null for live data. */
  initialSelectedNodeId?: string | null;
  /** Default toolbar mode. */
  initialMode?: ToolbarMode;
  /** Optional empty-state message when edges is empty. */
  emptyMessage?: string;
  /** Called whenever the selected node changes. */
  onNodeSelect?: (node: LineageNode | null) => void;
}

const TOOLBAR_MODES: { value: ToolbarMode; label: string; help: string }[] = [
  { value: 'all', label: 'All', help: 'Show every node and edge with no highlighting.' },
  { value: 'provenance', label: 'Provenance', help: 'From the selected node, highlight ancestors — what this depended on.' },
  { value: 'blast-radius', label: 'Blast radius', help: 'From the selected node, highlight descendants — what depends on this.' },
  { value: 'replay-chain', label: 'Replay chain', help: 'Follow only replayed_from edges to trace replay ancestry.' },
];

const FIXTURE_DEFAULT_SELECTION = 'asset:code-review-prompt@0.1.0';

export default function LineageGraphIsland(props: LineageGraphProps = {}) {
  return (
    <ReactFlowProvider>
      <LineageGraphInner {...props} />
    </ReactFlowProvider>
  );
}

function LineageGraphInner({
  edges: edgesInput,
  assets: assetsInput,
  initialSelectedNodeId,
  initialMode = 'blast-radius',
  emptyMessage = 'No lineage edges to display. Capture a run that consumes registered assets to build the graph.',
  onNodeSelect,
}: LineageGraphProps) {
  const usingFixture = edgesInput === undefined;
  const sourceEdges = edgesInput ?? fixtureEdges;
  const sourceAssets = assetsInput ?? fixtureRegistry.assets;
  const defaultSelection = initialSelectedNodeId !== undefined
    ? initialSelectedNodeId
    : (usingFixture ? FIXTURE_DEFAULT_SELECTION : null);

  const { fitView } = useReactFlow();

  const baseGraph = useMemo(() => {
    if (sourceEdges.length === 0) return { nodes: [] as LineageNode[], edges: [] as LineageEdge[] };
    const g = buildGraph(sourceEdges, sourceAssets);
    g.nodes = layoutGraph(g.nodes, g.edges);
    return g;
  }, [sourceEdges, sourceAssets]);

  const [nodes, setNodes, onNodesChange] = useNodesState<LineageNode>(baseGraph.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<LineageEdge>(baseGraph.edges);
  const [mode, setMode] = useState<ToolbarMode>(initialMode);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(defaultSelection);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const prevMode = useRef<ToolbarMode>(mode);

  // Reset state when input data changes (e.g. dashboard refresh)
  useEffect(() => {
    setNodes(baseGraph.nodes);
    setEdges(baseGraph.edges);
  }, [baseGraph, setNodes, setEdges]);

  useEffect(() => {
    if (!onNodeSelect) return;
    const n = baseGraph.nodes.find((nd) => nd.id === selectedNodeId) ?? null;
    onNodeSelect(n);
  }, [selectedNodeId, baseGraph.nodes, onNodeSelect]);

  // Auto-fit when mode changes so the relevant subgraph fills the viewport
  useEffect(() => {
    if (prevMode.current === mode) return;
    prevMode.current = mode;
    const t = setTimeout(() => fitView({ padding: 0.2, duration: 400 }), 60);
    return () => clearTimeout(t);
  }, [mode, fitView]);

  // Esc exits fullscreen
  useEffect(() => {
    if (!isFullscreen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setIsFullscreen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isFullscreen]);

  // Recompute highlights whenever mode or selection changes
  useEffect(() => {
    const { nodeIds, edgeIds } = highlightSubgraph(mode, selectedNodeId, baseGraph.edges);

    setNodes((current) =>
      current.map((n) => ({
        ...n,
        data: {
          ...n.data,
          dimmed: mode !== 'all' && nodeIds.size > 0 && !nodeIds.has(n.id),
          selected: n.id === selectedNodeId,
        },
      })),
    );

    setEdges((current) =>
      current.map((e) => {
        const dimmed = mode !== 'all' && edgeIds.size > 0 && !edgeIds.has(e.id);
        const color = EDGE_TYPE_COLOR[e.data!.edge_type];
        return {
          ...e,
          animated: e.id === Array.from(edgeIds)[0] && mode === 'replay-chain',
          style: {
            stroke: color,
            strokeWidth: edgeIds.has(e.id) ? 2.2 : 1.4,
            opacity: dimmed ? 0.18 : 1,
            transition: 'opacity 240ms, stroke-width 240ms',
          },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color,
            width: 16,
            height: 16,
          },
          label: edgeIds.has(e.id) || mode === 'all' ? EDGE_TYPE_LABEL[e.data!.edge_type] : undefined,
          labelStyle: { fill: 'var(--color-text-muted)', fontSize: 10, fontFamily: 'var(--font-mono)' },
          labelBgPadding: [4, 2],
          labelBgBorderRadius: 4,
          labelBgStyle: { fill: 'var(--color-bg-sunken)', fillOpacity: 0.92 },
        };
      }),
    );
  }, [mode, selectedNodeId, baseGraph.edges, setNodes, setEdges]);

  const onNodeClick: NodeMouseHandler<Node<LineageNodeData>> = (_event, node) => {
    setSelectedNodeId(node.id);
  };

  const onNodeDoubleClick: NodeMouseHandler<Node<LineageNodeData>> = (_event, node) => {
    setSelectedNodeId(node.id);
  };

  const onPaneClick = () => {
    setSelectedNodeId(null);
  };

  const selectedNode = nodes.find((n) => n.id === selectedNodeId) ?? null;
  const selectedEdges = baseGraph.edges.filter((e) => e.source === selectedNodeId || e.target === selectedNodeId);

  if (baseGraph.nodes.length === 0) {
    return <EmptyState message={emptyMessage ?? 'No lineage edges to display.'} />;
  }

  return (
    <div className={clsx(
      'grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-4',
      isFullscreen ? 'fixed inset-0 z-50 bg-[var(--color-bg)] p-4' : 'h-[640px]',
    )}>
      <div className="relative rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] overflow-hidden">
        {/* Toolbar */}
        <div className="absolute top-3 left-3 z-10 inline-flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)]/90 backdrop-blur p-1 shadow-lg">
          {TOOLBAR_MODES.map((m) => (
            <button
              key={m.value}
              type="button"
              onClick={() => setMode(m.value)}
              title={m.help}
              className={clsx(
                'px-3 py-1.5 text-xs rounded-md transition-colors',
                mode === m.value
                  ? 'bg-[var(--color-bg-raised)] text-[var(--color-text)] shadow-sm'
                  : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)]',
              )}
              aria-pressed={mode === m.value}
            >
              {m.label}
            </button>
          ))}
          <span className="w-px h-4 bg-[var(--color-border)] mx-0.5" aria-hidden="true" />
          <button
            type="button"
            onClick={() => setIsFullscreen((f) => !f)}
            title={isFullscreen ? 'Exit fullscreen (Esc)' : 'Expand to fullscreen'}
            aria-label={isFullscreen ? 'Exit fullscreen' : 'Expand to fullscreen'}
            className="px-2 py-1.5 rounded-md transition-colors text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)]"
            aria-pressed={isFullscreen}
          >
            {isFullscreen ? (
              <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4.5 1.5H2a.5.5 0 0 0-.5.5v2.5M8.5 1.5H11a.5.5 0 0 1 .5.5v2.5M4.5 11.5H2a.5.5 0 0 1-.5-.5V8.5M8.5 11.5H11a.5.5 0 0 0 .5-.5V8.5" />
              </svg>
            ) : (
              <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M1.5 4.5V2a.5.5 0 0 1 .5-.5h2.5M8.5 1.5H11a.5.5 0 0 1 .5.5v2.5M1.5 8.5V11a.5.5 0 0 0 .5.5h2.5M11.5 8.5V11a.5.5 0 0 1-.5.5H8.5" />
              </svg>
            )}
          </button>
        </div>

        <div className="absolute bottom-3 left-3 z-10 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)]/90 backdrop-blur p-3 text-xs">
          <div className="font-semibold text-[var(--color-text-muted)] uppercase tracking-wider mb-2">Edge types</div>
          <div className="space-y-1">
            {(Object.keys(EDGE_TYPE_COLOR) as Array<keyof typeof EDGE_TYPE_COLOR>).map((k) => (
              <div key={k} className="flex items-center gap-2 text-[var(--color-text-muted)]">
                <span className="w-3 h-0.5 rounded" style={{ background: EDGE_TYPE_COLOR[k] }} />
                <span className="font-mono text-[10px]">{k}</span>
              </div>
            ))}
          </div>
        </div>

        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          onNodeDoubleClick={onNodeDoubleClick}
          onPaneClick={onPaneClick}
          zoomOnDoubleClick={false}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          proOptions={{ hideAttribution: true }}
          minZoom={0.05}
          maxZoom={2}
          nodesDraggable={false}
          nodesConnectable={false}
          colorMode="dark"
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={24}
            size={1}
            color="var(--color-border)"
          />
          <Controls position="bottom-right" showInteractive={false} />
          <MiniMap
            position="top-right"
            nodeColor={(node) => {
              const d = node.data as LineageNodeData;
              if (d.kind === 'run') return '#6366f1';
              if (d.status === 'promoted') return '#22c55e';
              return '#64748b';
            }}
            nodeStrokeWidth={0}
            maskColor="rgba(0,0,0,0.55)"
            style={{
              background: 'hsl(220 13% 10%)',
              border: '1px solid hsl(220 13% 22%)',
              borderRadius: '8px',
            }}
            zoomable
            pannable
          />
        </ReactFlow>
      </div>

      {/* Side panel */}
      <aside className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5 overflow-y-auto">
        {selectedNode ? (
          <NodeDetail node={selectedNode} edges={selectedEdges} />
        ) : (
          <EmptyDetail />
        )}
      </aside>
    </div>
  );
}

function EmptyDetail() {
  return (
    <div className="text-sm text-[var(--color-text-muted)] leading-relaxed">
      <p className="font-medium text-[var(--color-text)] mb-2">No node selected</p>
      <p>
        Click a node to see its lineage edges, payload, and the surrounding subgraph.
      </p>
      <p className="mt-4">
        Try clicking <code className="text-[var(--color-text)] font-mono text-xs bg-[var(--color-bg-sunken)] px-1.5 py-0.5 rounded">code-review-prompt</code>
        with the <strong>Blast radius</strong> mode active to see which runs depend on it.
      </p>
    </div>
  );
}

function NodeDetail({ node, edges }: { node: LineageNode; edges: LineageEdge[] }) {
  const incoming = edges.filter((e) => e.target === node.id);
  const outgoing = edges.filter((e) => e.source === node.id);

  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs uppercase tracking-wider text-[var(--color-text-faint)]">{node.data.kind}</span>
        {node.data.status && (
          <span className={clsx(
            'px-1.5 py-0.5 rounded text-[10px] font-medium',
            node.data.status === 'promoted' || node.data.status === 'success'
              ? 'bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)] text-[var(--color-status-success)]'
              : 'bg-[color-mix(in_oklab,var(--color-status-pending)_5%,transparent)] text-[var(--color-status-pending)]',
          )}>
            {node.data.status}
          </span>
        )}
      </div>
      <h3 className="text-base font-medium text-[var(--color-text)] break-words font-mono">
        {node.data.label}
      </h3>
      {node.data.subLabel && (
        <p className="mt-1 text-xs text-[var(--color-text-muted)] font-mono break-words">{node.data.subLabel}</p>
      )}

      <hr className="my-4 border-[var(--color-border)]" />

      <div className="space-y-3">
        {outgoing.length > 0 && (
          <div>
            <div className="text-xs uppercase tracking-wider text-[var(--color-text-faint)] mb-2">
              Outgoing ({outgoing.length})
            </div>
            <ul className="space-y-1">
              {outgoing.map((e) => (
                <li key={e.id} className="flex items-start gap-2 text-xs">
                  <span
                    className="w-2 h-2 rounded-full mt-1 shrink-0"
                    style={{ background: EDGE_TYPE_COLOR[e.data!.edge_type] }}
                  />
                  <div className="min-w-0">
                    <div className="font-mono text-[var(--color-text-muted)]">{e.data!.edge_type}</div>
                    <div className="font-mono text-[10px] text-[var(--color-text-faint)] break-words">→ {shorten(e.target)}</div>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
        {incoming.length > 0 && (
          <div>
            <div className="text-xs uppercase tracking-wider text-[var(--color-text-faint)] mb-2">
              Incoming ({incoming.length})
            </div>
            <ul className="space-y-1">
              {incoming.map((e) => (
                <li key={e.id} className="flex items-start gap-2 text-xs">
                  <span
                    className="w-2 h-2 rounded-full mt-1 shrink-0"
                    style={{ background: EDGE_TYPE_COLOR[e.data!.edge_type] }}
                  />
                  <div className="min-w-0">
                    <div className="font-mono text-[var(--color-text-muted)]">{e.data!.edge_type}</div>
                    <div className="font-mono text-[10px] text-[var(--color-text-faint)] break-words">← {shorten(e.source)}</div>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
        {incoming.length === 0 && outgoing.length === 0 && (
          <p className="text-xs text-[var(--color-text-muted)]">No edges from this node.</p>
        )}
      </div>
    </div>
  );
}

function shorten(id: string): string {
  const colon = id.indexOf(':');
  const kind = id.slice(0, colon);
  const ref = id.slice(colon + 1);
  if (kind === 'run') return `run:${ref.slice(0, 8)}…`;
  if (kind === 'artifact') return `artifact:${ref.slice(-30)}`;
  return id;
}
