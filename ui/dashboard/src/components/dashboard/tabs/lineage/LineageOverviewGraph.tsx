/**
 * Overview level of the lineage view: one node per node kind, one edge per
 * (kind, relationship, kind) with its count. Always a handful of nodes, whatever the graph size.
 * Uses the dashboard's existing React Flow + dagre stack; no new graph library.
 */
import { useMemo } from 'react';
import dagre from '@dagrejs/dagre';
import { ReactFlow, Background, BackgroundVariant, MarkerType, Position, type Edge, type Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { ClusterEdge, KindCluster } from '../../../../lib/lineageScope';

const W = 180;
const H = 56;

export function plural(kind: string, n: number): string {
  return `${n} ${kind}${n === 1 ? '' : 's'}`;
}

export default function LineageOverviewGraph({
  clusters,
  links,
  onPick,
}: {
  clusters: KindCluster[];
  links: ClusterEdge[];
  onPick: (kind: string) => void;
}) {
  const { nodes, edges } = useMemo(() => {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: 'LR', ranksep: 160, nodesep: 60, marginx: 24, marginy: 24 });
    g.setDefaultEdgeLabel(() => ({}));
    for (const c of clusters) g.setNode(c.kind, { width: W, height: H });
    // Layout needs one edge per kind pair; self-links (run -> run) only affect labels.
    for (const l of links) if (l.sourceKind !== l.targetKind) g.setEdge(l.sourceKind, l.targetKind);
    dagre.layout(g);
    const ns: Node[] = clusters.map((c) => {
      const p = g.node(c.kind);
      return {
        id: c.kind,
        position: { x: p.x - W / 2, y: p.y - H / 2 },
        data: { label: plural(c.kind, c.count) },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        style: { width: W, fontFamily: 'var(--font-mono)', fontSize: 12 },
      };
    });
    const es: Edge[] = links.map((l, i) => ({
      id: `l${i}`,
      source: l.sourceKind,
      target: l.targetKind,
      label: `${l.edgeType} ×${l.count}`,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      labelStyle: { fontSize: 10, fontFamily: 'var(--font-mono)' },
    }));
    return { nodes: ns, edges: es };
  }, [clusters, links]);

  return (
    <div className="h-[360px] rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={(_e, n) => onPick(n.id)}
        fitView
        fitViewOptions={{ padding: '10%' }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        zoomOnDoubleClick={false}
        forceColorMode="dark"
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="var(--color-border)" />
      </ReactFlow>
    </div>
  );
}
