import dagre from '@dagrejs/dagre';
import { Position, type Edge, type Node } from '@xyflow/react';
import type { LineageEdgeRecord, AssetRecord } from './fixtures';

export type NodeKind = 'run' | 'asset' | 'artifact';

export interface LineageNodeData extends Record<string, unknown> {
  kind: NodeKind;
  ref: string;
  label: string;
  subLabel?: string;
  status?: 'promoted' | 'development' | 'success' | 'failure';
  asset_type?: AssetRecord['asset_type'];
}

export type LineageNode = Node<LineageNodeData>;
export type LineageEdge = Edge<{ edge_type: LineageEdgeRecord['edge_type']; capsule_run_id: string; confidence: string; edge_id: string }>;

const NODE_W = 220;
const NODE_H = 64;

function normalizeSemver(v: string): string {
  if (!v || v === 'latest') return v;
  const prefix = v.startsWith('v') ? 'v' : '';
  const bare = v.replace(/^v/, '');
  const parts = bare.split('.');
  while (parts.length < 3) parts.push('0');
  return prefix + parts.join('.');
}

function findAsset(assets: AssetRecord[], name: string, version: string): AssetRecord | undefined {
  // 1. Exact match
  let a = assets.find((r) => r.name === name && r.version === version);
  if (a) return a;
  // 2. @latest → newest by creation order (assets are already sorted DESC by the API)
  if (version === 'latest') return assets.find((r) => r.name === name);
  // 3. Normalized semver (v1 → v1.0.0, 1.0 → 1.0.0)
  const norm = normalizeSemver(version);
  if (norm !== version) a = assets.find((r) => r.name === name && r.version === norm);
  if (a) return a;
  // 4. Cross-normalize: DB version may differ in v-prefix or zero-padding
  return assets.find((r) => r.name === name && normalizeSemver(r.version) === norm);
}

export function buildGraph(edges: LineageEdgeRecord[], assets: AssetRecord[]): { nodes: LineageNode[]; edges: LineageEdge[] } {
  const seen = new Map<string, LineageNode>();

  const ensureNode = (id: string): void => {
    if (seen.has(id)) return;
    const colon = id.indexOf(':');
    const kind = id.slice(0, colon) as NodeKind;
    const ref = id.slice(colon + 1);

    let label = ref;
    let subLabel: string | undefined;
    let status: LineageNodeData['status'];
    let asset_type: LineageNodeData['asset_type'];

    if (kind === 'run') {
      label = ref.slice(0, 10) + '…';
      subLabel = 'run';
    } else if (kind === 'asset') {
      const [name, version] = ref.split('@');
      label = name;
      subLabel = version;
      const asset = findAsset(assets, name, version ?? '');
      if (asset) {
        status = asset.status;
        asset_type = asset.asset_type;
      }
    } else if (kind === 'artifact') {
      const [, path] = ref.split(':');
      label = path ?? ref;
      subLabel = 'artifact';
    }

    seen.set(id, {
      id,
      type: 'lineage',
      position: { x: 0, y: 0 },
      data: { kind, ref, label, subLabel, status, asset_type },
    });
  };

  const builtEdges: LineageEdge[] = edges.map((e) => {
    ensureNode(e.source);
    ensureNode(e.target);
    return {
      id: e.edge_id,
      source: e.source,
      target: e.target,
      type: 'lineage',
      data: {
        edge_id: e.edge_id,
        edge_type: e.edge_type,
        capsule_run_id: e.capsule_run_id,
        confidence: e.confidence,
      },
    };
  });

  const builtNodes = Array.from(seen.values());
  return { nodes: builtNodes, edges: builtEdges };
}

export function layoutGraph(nodes: LineageNode[], edges: LineageEdge[]): LineageNode[] {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', ranksep: 120, nodesep: 28, marginx: 32, marginy: 32 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: NODE_W, height: NODE_H });
  for (const e of edges) g.setEdge(e.source, e.target);
  dagre.layout(g);

  return nodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    };
  });
}

export type ToolbarMode = 'all' | 'provenance' | 'blast-radius' | 'replay-chain';

export function highlightSubgraph(
  mode: ToolbarMode,
  selectedNodeId: string | null,
  edges: LineageEdge[],
): { nodeIds: Set<string>; edgeIds: Set<string> } {
  const nodeIds = new Set<string>();
  const edgeIds = new Set<string>();

  if (mode === 'all' || !selectedNodeId) {
    return { nodeIds, edgeIds };
  }

  // Build adjacency
  const outgoing = new Map<string, LineageEdge[]>();
  const incoming = new Map<string, LineageEdge[]>();
  for (const e of edges) {
    if (!outgoing.has(e.source)) outgoing.set(e.source, []);
    outgoing.get(e.source)!.push(e);
    if (!incoming.has(e.target)) incoming.set(e.target, []);
    incoming.get(e.target)!.push(e);
  }

  const filterFn = (e: LineageEdge): boolean => {
    if (mode === 'replay-chain') return e.data?.edge_type === 'replayed_from';
    return true;
  };

  const walk = (start: string, direction: 'out' | 'in') => {
    const queue = [start];
    const visited = new Set<string>([start]);
    nodeIds.add(start);
    while (queue.length) {
      const cur = queue.shift()!;
      const adj = (direction === 'out' ? outgoing.get(cur) : incoming.get(cur)) ?? [];
      for (const e of adj) {
        if (!filterFn(e)) continue;
        edgeIds.add(e.id);
        const next = direction === 'out' ? e.target : e.source;
        if (!visited.has(next)) {
          visited.add(next);
          nodeIds.add(next);
          queue.push(next);
        }
      }
    }
  };

  if (mode === 'provenance') {
    // Ancestors: follow outgoing edges (run → consumed asset; replay → original; artifact → producing run)
    walk(selectedNodeId, 'out');
  } else if (mode === 'blast-radius') {
    // Descendants: follow incoming edges (asset ← consumed-by ← run; run ← replayed-from ← replay)
    walk(selectedNodeId, 'in');
  } else if (mode === 'replay-chain') {
    // Both directions, but only across replayed_from edges
    walk(selectedNodeId, 'out');
    walk(selectedNodeId, 'in');
  }

  return { nodeIds, edgeIds };
}

export const EDGE_TYPE_COLOR: Record<LineageEdgeRecord['edge_type'], string> = {
  consumed: 'var(--color-edge-consumed)',
  produced_by: 'var(--color-edge-produced-by)',
  replayed_from: 'var(--color-edge-replayed-from)',
  derived_from: 'var(--color-edge-derived-from)',
  evaluated_by: 'var(--color-edge-evaluated-by)',
  attested_by: 'var(--color-edge-attested-by)',
  contains: 'var(--color-edge-contains)',
  delegated_to: 'var(--color-edge-delegated-to)',
  spawned: 'var(--color-edge-spawned)',
};

export const EDGE_TYPE_LABEL: Record<LineageEdgeRecord['edge_type'], string> = {
  consumed: 'consumed',
  produced_by: 'produced',
  replayed_from: 'replayed_from',
  derived_from: 'derived_from',
  evaluated_by: 'evaluated_by',
  attested_by: 'attested_by',
  contains: 'contains',
  delegated_to: 'delegated_to',
  spawned: 'spawned',
};
