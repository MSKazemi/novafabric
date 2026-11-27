/**
 * Text alternative to the lineage graph: the same nodes and edges as two plain data tables.
 *
 * Deliberately a plain <table>, not an ARIA grid: rows are not focusable and take no arrow
 * keys, so a screen reader's own table navigation (and browse mode) works unchanged. Every
 * action is a real button: in the node table, the reference selects the node (aria-pressed)
 * and "Scope here" scopes the view to it. Tab therefore stops on each row's buttons; the
 * table is bounded by the same node budget as the graph.
 */
import type { ReactNode } from 'react';
import { splitNodeId, type ScopeEdge } from '../../../../lib/lineageScope';

export interface TableNode {
  id: string;
  /** Hop distance from the scope focus; undefined when unscoped. */
  hop?: number;
  degree: number;
}

const TH = 'text-left font-medium text-[var(--color-text-faint)] uppercase tracking-wider text-[10px] px-2 py-1.5';
const TD = 'px-2 py-1.5 align-top';
const ROW = 'border-t border-[var(--color-border)]';
const BTN = 'text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)] hover:text-[var(--color-text)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]';
const REF_BTN = 'text-left font-mono break-all rounded px-1 -mx-1 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]';

function TableFrame({ caption, children }: { caption: ReactNode; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-x-auto">
      <table className="w-full text-xs">
        <caption className="text-left px-3 py-2 text-[11px] text-[var(--color-text-muted)]">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function NodeTable({
  nodes,
  focus,
  selected,
  onSelect,
  onScope,
  scopeEnabled = true,
}: {
  nodes: TableNode[];
  focus: string | null;
  selected: string | null;
  onSelect: (id: string) => void;
  onScope: (id: string) => void;
  /** False at a level where scoping does not apply. */
  scopeEnabled?: boolean;
}) {
  const scoped = focus !== null;
  return (
    <TableFrame
      caption={
        <>
          <span className="font-semibold text-[var(--color-text)]">Nodes</span> ({nodes.length} shown).
          {' '}Press a reference to select that node; press <em>Scope here</em> to show only its neighbourhood.
        </>
      }
    >
      <thead>
        <tr>
          <th scope="col" className={TH}>Kind</th>
          <th scope="col" className={TH}>Reference</th>
          {scoped && <th scope="col" className={TH}>Hops from scope</th>}
          <th scope="col" className={TH}>Edges shown</th>
          <th scope="col" className={TH}><span className="sr-only">Actions</span></th>
        </tr>
      </thead>
      <tbody>
        {nodes.map((n) => {
          const { kind, ref } = splitNodeId(n.id);
          const isSel = n.id === selected;
          return (
            <tr
              key={n.id}
              className={`${ROW} ${isSel ? 'bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]' : ''}`}
            >
              <td className={`${TD} font-mono text-[var(--color-text-faint)]`}>{kind || '—'}</td>
              <td className={`${TD} text-[var(--color-text)]`}>
                <button
                  type="button"
                  aria-pressed={isSel}
                  onClick={() => onSelect(n.id)}
                  className={REF_BTN}
                >
                  {ref}
                </button>
                {n.id === focus && <span className="ml-2 text-[10px] text-[var(--color-accent)]">(scope)</span>}
              </td>
              {scoped && <td className={`${TD} font-mono`}>{n.hop ?? '—'}</td>}
              <td className={`${TD} font-mono`}>{n.degree}</td>
              <td className={`${TD} text-right whitespace-nowrap`}>
                {scopeEnabled && n.id !== focus && (
                  <button type="button" className={BTN} onClick={() => onScope(n.id)}>
                    Scope here<span className="sr-only"> ({ref})</span>
                  </button>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </TableFrame>
  );
}

export function EdgeTable({ edges }: { edges: ScopeEdge[] }) {
  return (
    <TableFrame
      caption={
        <>
          <span className="font-semibold text-[var(--color-text)]">Edges</span> ({edges.length} shown).
          {' '}An edge is a relationship a capsule recorded (see Confidence), read as
          {' '}<em>source → relationship → target</em>. It is an association, not a claim of cause.
        </>
      }
    >
      <thead>
        <tr>
          <th scope="col" className={TH}>Source</th>
          <th scope="col" className={TH}>Relationship</th>
          <th scope="col" className={TH}>Target</th>
          <th scope="col" className={TH}>Confidence</th>
          <th scope="col" className={TH}>Recorded by run</th>
          <th scope="col" className={TH}>Recorded at</th>
        </tr>
      </thead>
      <tbody>
        {edges.map((e) => (
          <tr key={e.edge_id} className={ROW}>
            <td className={`${TD} font-mono break-all`}>{e.source}</td>
            <td className={`${TD} font-mono text-[var(--color-text-muted)]`}>{e.edge_type}</td>
            <td className={`${TD} font-mono break-all`}>{e.target}</td>
            <td className={`${TD} font-mono`}>{e.confidence || '—'}</td>
            <td className={`${TD} font-mono break-all text-[var(--color-text-faint)]`}>{e.capsule_run_id || '—'}</td>
            <td className={`${TD} font-mono text-[var(--color-text-faint)]`}>{e.created_at || '—'}</td>
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}
