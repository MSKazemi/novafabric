/**
 * Lineage explorer (#274): bounded levels with an "N more — narrow the scope" control, a
 * scoped neighbourhood read from the URL with its CLI equivalent, a plain-table alternative
 * whose actions are real buttons, a scope-keyed store cross-check, and a call level that never
 * shows call content.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { LineageEdgeRecord } from '@/lib/fixtures';

type Rec = { node_id: string; kind: string; ref: string; payload: string };
const provenance = vi.fn(async (ref: string, depth?: number, _edgeType?: string, _kind?: string) => ({ ref, depth: depth ?? 5, ancestors: [] as Rec[] }));
const blastRadius = vi.fn(async (ref: string, depth?: number, _edgeType?: string, _kind?: string) => ({ ref, depth: depth ?? 5, descendants: [] as Rec[] }));
const getRun = vi.fn(async (id: string) => ({
  run_id: id, capsule_path: `/c/${id}`, manifest: {}, trace: [], lineage: [], assets: [], inputs: [], outputs: [],
  model_calls: [{
    model_call_id: 'mc-1', 'gen_ai.request.model': 'model-x', status: 'ok', started_at: '2026-01-01T00:00:00Z', duration_ms: 7,
    'gen_ai.request.messages': [{ role: 'user', content: 'TOP SECRET PROMPT' }],
  }],
  tool_calls: [{ tool_call_id: 'tc-1', tool_name: 'grep', status: 'ok', arguments: { q: 'TOP SECRET ARG' } }],
}));

vi.mock('@/lib/api', () => ({
  api: {
    provenance: (ref: string, depth?: number, edgeType?: string, kind?: string) => provenance(ref, depth, edgeType, kind),
    blastRadius: (ref: string, depth?: number, edgeType?: string, kind?: string) => blastRadius(ref, depth, edgeType, kind),
    getRun: (id: string) => getRun(id),
  },
  ServeApiError: class ServeApiError extends Error {},
}));
// React Flow needs a real layout engine; the graph itself is covered by e2e. Record what
// the explorer hands it instead.
const graphCalls: Array<{ edges: LineageEdgeRecord[] }> = [];
vi.mock('@/components/lineage/LineageGraph', () => ({
  default: (props: { edges: LineageEdgeRecord[] }) => {
    graphCalls.push(props);
    return <div data-testid="lineage-graph">{props.edges.length} edges drawn</div>;
  },
}));
vi.mock('@/components/dashboard/tabs/lineage/LineageOverviewGraph', () => ({
  default: () => <div data-testid="overview-graph" />,
  plural: (kind: string, n: number) => `${n} ${kind}${n === 1 ? '' : 's'}`,
}));

import LineageExplorer from '@/components/dashboard/tabs/lineage/LineageExplorer';

const edge = (id: string, source: string, edge_type: LineageEdgeRecord['edge_type'], target: string): LineageEdgeRecord => ({
  edge_id: id, source, target, edge_type, capsule_run_id: 'r1', confidence: 'observed', created_at: '2026-01-01T00:00:00Z', direction: 'source_to_target',
});

// 200 runs that each consumed the same asset: 201 nodes.
const BIG = Array.from({ length: 200 }, (_, i) => edge(`b${i}`, `run:r${String(i).padStart(3, '0')}`, 'consumed', 'asset:hub@1'));
const SMALL = [
  edge('e1', 'run:r1', 'consumed', 'asset:a@1'),
  edge('e2', 'run:r2', 'consumed', 'asset:a@1'),
  edge('e3', 'run:r3', 'replayed_from', 'run:r1'),
];

function setUrl(qs: string) {
  window.history.replaceState({}, '', `/dashboard${qs ? `?${qs}` : ''}`);
}

function explorer(edges: LineageEdgeRecord[], extra: Partial<Parameters<typeof LineageExplorer>[0]> = {}) {
  return <LineageExplorer edges={edges} assets={[]} loadedCount={edges.length} truncated={false} filtered={false} {...extra} />;
}

function renderExplorer(edges: LineageEdgeRecord[], extra: Partial<Parameters<typeof LineageExplorer>[0]> = {}) {
  return render(explorer(edges, extra));
}

beforeEach(() => {
  setUrl('');
  graphCalls.length = 0;
  provenance.mockClear();
  blastRadius.mockClear();
  getRun.mockClear();
});

describe('bounded entity level', () => {
  it('lists at most 60 nodes in the table and offers to narrow the scope', () => {
    setUrl('lineage_view=table');
    renderExplorer(BIG);
    const nodes = screen.getByRole('table', { name: /Nodes/ });
    expect(within(nodes).getAllByRole('row')).toHaveLength(61); // header + 60
    expect(screen.getByText(/141 more nodes/)).toBeInTheDocument();
    expect(screen.getByText(/narrow the scope/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show 60 more' }));
    expect(within(screen.getByRole('table', { name: /Nodes/ })).getAllByRole('row')).toHaveLength(121);
  });

  it('never hands the graph more than the budget of nodes', () => {
    renderExplorer(BIG);
    const last = graphCalls[graphCalls.length - 1];
    const ids = new Set(last.edges.flatMap((x) => [x.source, x.target]));
    expect(ids.size).toBeLessThanOrEqual(60);
    expect(screen.getByText(/more nodes/)).toBeInTheDocument();
  });

  it('shows the whole-graph export as the unscoped CLI equivalent', () => {
    renderExplorer(SMALL);
    expect(screen.getByText('$ nova lineage export-graph')).toBeInTheDocument();
  });
});

describe('scoped view', () => {
  it('reads the scope from the URL, draws only the neighbourhood and shows its CLI', async () => {
    setUrl('lineage_focus=run:r1&lineage_depth=1&lineage_view=table');
    renderExplorer(SMALL);
    const rows = within(screen.getByRole('table', { name: /Nodes/ })).getAllByRole('row').slice(1);
    const refs = rows.map((r) => within(r).getAllByRole('cell')[1].textContent);
    // r2 shares an asset with r1 but is neither an ancestor nor a descendant.
    expect(refs.some((t) => t?.startsWith('r2'))).toBe(false);
    expect(refs.some((t) => t?.startsWith('a@1'))).toBe(true);
    expect(refs.some((t) => t?.startsWith('r3'))).toBe(true);
    expect(screen.getByText('$ nova lineage export-graph --ref r1 --kind run --depth 1')).toBeInTheDocument();
    expect(screen.getByText('$ nova lineage provenance r1 --kind run --depth 1')).toBeInTheDocument();
    expect(screen.getByText('$ nova lineage blast-radius r1 --kind run --depth 1')).toBeInTheDocument();
    // The store is asked with the same --kind the copied CLI commands carry.
    await waitFor(() => expect(provenance).toHaveBeenCalledWith('r1', 1, undefined, 'run'));
    expect(blastRadius).toHaveBeenCalledWith('r1', 1, undefined, 'run');
  });

  it('queries the store once per scope, not on every change to the loaded edges', async () => {
    setUrl('lineage_focus=run:r1&lineage_view=table');
    const { rerender } = renderExplorer(SMALL);
    await waitFor(() => expect(provenance).toHaveBeenCalledTimes(1));
    // Typing in the tab's search box hands the explorer a new filtered array each keystroke.
    rerender(explorer([...SMALL], { filtered: true }));
    rerender(explorer(SMALL.slice(0, 2), { filtered: true }));
    rerender(explorer([...SMALL]));
    await new Promise((r) => setTimeout(r, 400));
    expect(provenance).toHaveBeenCalledTimes(1);
    expect(blastRadius).toHaveBeenCalledTimes(1);
  });

  it('changes depth through the URL and the CLI follows', () => {
    setUrl('lineage_focus=run:r1&lineage_view=table');
    renderExplorer(SMALL);
    fireEvent.change(screen.getByRole('combobox', { name: 'Neighbourhood depth in hops' }), { target: { value: '3' } });
    expect(new URLSearchParams(window.location.search).get('lineage_depth')).toBe('3');
    expect(screen.getByText('$ nova lineage export-graph --ref r1 --kind run --depth 3')).toBeInTheDocument();
  });

  it('names store nodes the loaded edges do not contain', async () => {
    provenance.mockResolvedValueOnce({ ref: 'r1', depth: 2, ancestors: [{ node_id: 'n1', kind: 'asset', ref: 'a@1', payload: '{}' }, { node_id: 'n9', kind: 'asset', ref: 'old@0', payload: '{}' }] });
    setUrl('lineage_focus=run:r1&lineage_view=table');
    renderExplorer(SMALL);
    expect(await screen.findByText(/not in the loaded edges and not drawn: asset:old@0/)).toBeInTheDocument();
  });

  it('reports a failed store check and retries it', async () => {
    blastRadius.mockRejectedValueOnce(new Error('boom'));
    setUrl('lineage_focus=run:r1&lineage_view=table');
    renderExplorer(SMALL);
    expect(await screen.findByText(/Could not check the neighbourhood against the store: boom/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(blastRadius).toHaveBeenCalledTimes(2));
  });

  it('says when the focus is in no loaded edge', () => {
    setUrl('lineage_focus=run:ghost&lineage_view=table');
    renderExplorer(SMALL);
    expect(screen.getByText(/is not an endpoint of any loaded edge/)).toBeInTheDocument();
  });
});

describe('table view', () => {
  it('is a plain table: rows are not focusable, every action is a button', () => {
    setUrl('lineage_view=table');
    renderExplorer(SMALL);
    const table = screen.getByRole('table', { name: /Nodes/ });
    expect(screen.queryByRole('grid')).toBeNull();
    for (const row of within(table).getAllByRole('row')) expect(row).not.toHaveAttribute('tabindex');
    expect(within(table).getAllByRole('button', { name: /^Scope here/ })).toHaveLength(4);
  });

  it('selects with the reference button and tells the tab, so the breadcrumb follows', () => {
    const onNodeSelect = vi.fn();
    setUrl('lineage_view=table');
    renderExplorer(SMALL, { onNodeSelect });
    const ref = screen.getByRole('button', { name: 'r3' });
    expect(ref).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(ref);
    expect(screen.getByRole('button', { name: 'r3' })).toHaveAttribute('aria-pressed', 'true');
    expect(onNodeSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'run:r3' }));
    // The toolbar now offers to scope to the selection.
    fireEvent.click(screen.getByRole('button', { name: 'Scope to selected node' }));
    expect(new URLSearchParams(window.location.search).get('lineage_focus')).toBe('run:r3');
  });

  it('scopes from a row button', () => {
    setUrl('lineage_view=table');
    renderExplorer(SMALL);
    fireEvent.click(screen.getByRole('button', { name: /^Scope here\s*\(a@1\)$/ }));
    expect(new URLSearchParams(window.location.search).get('lineage_focus')).toBe('asset:a@1');
  });

  it('lists edges with their recorded confidence and an association-not-cause caption', () => {
    setUrl('lineage_view=table');
    renderExplorer(SMALL);
    const edges = screen.getByRole('table', { name: /Edges/ });
    expect(within(edges).getAllByRole('row')).toHaveLength(4);
    expect(within(edges).getByText(/not a claim of cause/)).toBeInTheDocument();
  });

  it('switches between graph and table without losing the scope', () => {
    setUrl('lineage_focus=run:r1');
    renderExplorer(SMALL);
    expect(screen.getByTestId('lineage-graph')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Table' }));
    expect(screen.getByRole('table', { name: /Nodes/ })).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get('lineage_focus')).toBe('run:r1');
  });
});

describe('overview and call levels', () => {
  it('summarises kinds at the overview and zooms into one', () => {
    setUrl('lineage_zoom=overview&lineage_view=table');
    renderExplorer(SMALL);
    fireEvent.click(screen.getByRole('button', { name: 'Show 3 runs' }));
    const p = new URLSearchParams(window.location.search);
    expect(p.get('lineage_kind')).toBe('run');
    expect(p.get('lineage_zoom')).toBeNull(); // entity is the default level
  });

  it('keeps the call level unavailable without a run scope', () => {
    renderExplorer(SMALL);
    expect(screen.getByRole('button', { name: 'Calls' })).toBeDisabled();
  });

  it('lists a run\'s calls by metadata only, never their content', async () => {
    setUrl('lineage_focus=run:r1&lineage_zoom=call');
    renderExplorer(SMALL);
    expect(await screen.findByText('model-x')).toBeInTheDocument();
    expect(screen.getByText('grep')).toBeInTheDocument();
    expect(getRun).toHaveBeenCalledWith('r1');
    expect(document.body.textContent).not.toMatch(/TOP SECRET/);
  });
});
