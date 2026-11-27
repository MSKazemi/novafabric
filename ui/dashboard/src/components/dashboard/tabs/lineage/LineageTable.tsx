/**
 * Text alternative to the lineage graph: the same nodes and edges as two tables.
 *
 * Keyboard: each table is one tab stop (roving tabindex). ArrowUp / ArrowDown move between
 * rows, Home / End jump to the first / last row, Enter on a node row scopes the view to that
 * node, Space selects it. Every action is also a real button in the row.
 */
import { useCallback, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { splitNodeId, type ScopeEdge } from '../../../../lib/lineageScope';

export interface TableNode {
  id: string;
  /** Hop distance from the scope focus; undefined when unscoped. */
  hop?: number;
  degree: number;
}

function useRovingRows(count: number) {
  const [active, setActive] = useState(0);
  const rows = useRef<Array<HTMLTableRowElement | null>>([]);
  const focusRow = useCallback((i: number) => {
    const next = Math.max(0, Math.min(count - 1, i));
    setActive(next);
    rows.current[next]?.focus();
  }, [count]);
  const onKeyDown = useCallback((e: KeyboardEvent<HTMLTableRowElement>, i: number) => {
    if (e.target !== e.currentTarget) return; // keys inside a row's button stay with the button
    if (e.key === 'ArrowDown') { e.preventDefault(); focusRow(i + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); focusRow(i - 1); }
    else if (e.key === 'Home') { e.preventDefault(); focusRow(0); }
    else if (e.key === 'End') { e.preventDefault(); focusRow(count - 1); }
  }, [count, focusRow]);
  const rowProps = (i: number) => ({
    ref: (el: HTMLTableRowElement | null) => { rows.current[i] = el; },
    tabIndex: i === Math.min(active, count - 1) ? 0 : -1,
    onFocus: () => setActive(i),
  });
  return { rowProps, onKeyDown };
}

const TH = 'text-left font-medium text-[var(--color-text-faint)] uppercase tracking-wider text-[10px] px-2 py-1.5';
const TD = 'px-2 py-1.5 align-top';
const ROW = 'border-t border-[var(--color-border)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] focus-visible:ring-inset';
const BTN = 'text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)] hover:text-[var(--color-text)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]';

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
  const { rowProps, onKeyDown } = useRovingRows(nodes.length);
  const scoped = focus !== null;
  return (
    <TableFrame
      caption={
        <>
          <span className="font-semibold text-[var(--color-text)]">Nodes</span> ({nodes.length} shown).
          {' '}Arrow keys move between rows; Enter scopes to the row&apos;s node; Space selects it.
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
        {nodes.map((n, i) => {
          const { kind, ref } = splitNodeId(n.id);
          const isSel = n.id === selected;
          return (
            <tr
              key={n.id}
              {...rowProps(i)}
              aria-selected={isSel}
              onKeyDown={(e) => {
                onKeyDown(e, i);
                if (e.target !== e.currentTarget) return;
                if (e.key === 'Enter' && scopeEnabled) { e.preventDefault(); onScope(n.id); }
                else if (e.key === ' ') { e.preventDefault(); onSelect(n.id); }
              }}
              onClick={() => onSelect(n.id)}
              className={`${ROW} ${isSel ? 'bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]' : ''}`}
            >
              <td className={`${TD} font-mono text-[var(--color-text-faint)]`}>{kind || '—'}</td>
              <td className={`${TD} font-mono text-[var(--color-text)] break-all`}>
                {ref}
                {n.id === focus && <span className="ml-2 text-[10px] text-[var(--color-accent)]">(scope)</span>}
              </td>
              {scoped && <td className={`${TD} font-mono`}>{n.hop ?? '—'}</td>}
              <td className={`${TD} font-mono`}>{n.degree}</td>
              <td className={`${TD} text-right whitespace-nowrap`}>
                {scopeEnabled && n.id !== focus && (
                  <button type="button" className={BTN} onClick={(e) => { e.stopPropagation(); onScope(n.id); }}>
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
  const { rowProps, onKeyDown } = useRovingRows(edges.length);
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
        {edges.map((e, i) => (
          <tr
            key={e.edge_id}
            {...rowProps(i)}
            onKeyDown={(ev) => onKeyDown(ev, i)}
            className={ROW}
          >
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
