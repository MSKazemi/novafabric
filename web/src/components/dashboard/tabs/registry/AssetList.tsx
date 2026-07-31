/**
 * Virtualized lifecycle action table (BL-5) + bulk-promote bar + the honest
 * truncation footer.
 *
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen —
 * except the hand-rolled "Load more" row, which now renders through the
 * shared ADR-0199 `TruncationNotice` fed by the server's offset/limit `total`.
 * Rows carry checkboxes, sparklines, and status-conditional action clusters,
 * so the tab keeps its own `useVirtualizer` rather than adopting `DataTable`.
 */
import { useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import type { AssetSummary } from '../../../../lib/api';
import EvalSparkline, { type EvalHistoryEntry } from '../../EvalSparkline';
import TruncationNotice from '../../../ui/TruncationNotice';
import { writeResumeItem } from '../HomeTab';
import { StatusBadge } from './lifecycle';

export interface AssetListProps {
  visibleAssets: AssetSummary[];
  /** Assets loaded from the server so far (pre client-side filter). */
  loadedCount: number;
  totalAssets: number;
  selectedIds: Set<string>;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  sparklineHistory: Record<string, EvalHistoryEntry[]>;
  rowRefCallback: (el: HTMLTableCellElement | null, assetId: string) => void;
  versionsByName: Record<string, string[]>;
  onEval: (a: AssetSummary) => void;
  onPromote: (a: AssetSummary) => void;
  onCompareOpen: (assetName: string) => void;
  onRollback: (a: AssetSummary) => void;
  onApprovalOpen: (a: AssetSummary) => void;
  onUnregister: (a: AssetSummary) => void;
  bulkBusy: boolean;
  onBulkPromote: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
}

export default function AssetList({
  visibleAssets,
  loadedCount,
  totalAssets,
  selectedIds,
  setSelectedIds,
  sparklineHistory,
  rowRefCallback,
  versionsByName,
  onEval,
  onPromote,
  onCompareOpen,
  onRollback,
  onApprovalOpen,
  onUnregister,
  bulkBusy,
  onBulkPromote,
  hasMore,
  loadingMore,
  loadMore,
}: AssetListProps) {
  // Virtual scroll — BL-5: collapse DOM at 10K rows
  const tableContainerRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: visibleAssets.length,
    getScrollElement: () => tableContainerRef.current,
    estimateSize: () => 40,
    overscan: 10,
  });

  return (
    <>
      {/* Lifecycle action table — BL-5: virtual scroll to collapse DOM at 10K rows */}
      {visibleAssets.length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
          <table className="w-full text-xs" style={{ tableLayout: 'fixed' }}>
            <thead className="border-b border-[var(--color-border)] bg-[var(--color-bg-sunken)]">
              <tr>
                <th className="w-8 px-2">
                  <input
                    type="checkbox"
                    checked={visibleAssets.length > 0 && visibleAssets.every(a => selectedIds.has(a.id))}
                    onChange={e => {
                      if (e.target.checked) setSelectedIds(new Set(visibleAssets.map(a => a.id)));
                      else setSelectedIds(new Set());
                    }}
                    className="accent-[var(--color-accent)]"
                  />
                </th>
                <th className="text-left px-4 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Asset</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Type</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Status</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Trend</th>
                <th className="text-right px-4 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Actions</th>
              </tr>
            </thead>
          </table>
          <div
            ref={tableContainerRef}
            style={{ overflowY: 'auto', maxHeight: 600 }}
          >
            <table className="w-full text-xs" style={{ tableLayout: 'fixed' }}>
              <tbody style={{ display: 'block', position: 'relative', height: rowVirtualizer.getTotalSize() }}>
                {rowVirtualizer.getVirtualItems().map(virtualRow => {
                  const a = visibleAssets[virtualRow.index];
                  if (!a) return null;
                  return (
                    <tr
                      key={a.id}
                      data-index={virtualRow.index}
                      ref={rowVirtualizer.measureElement}
                      style={{ display: 'table', width: '100%', tableLayout: 'fixed', position: 'absolute', top: 0, left: 0, transform: `translateY(${virtualRow.start}px)` }}
                      className="hover:bg-[var(--color-bg-sunken)] transition-colors border-b border-[var(--color-border)]"
                    >
                      <td className="px-2 w-8">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(a.id)}
                          onChange={e => {
                            const next = new Set(selectedIds);
                            if (e.target.checked) next.add(a.id); else next.delete(a.id);
                            setSelectedIds(next);
                          }}
                          className="accent-[var(--color-accent)]"
                        />
                      </td>
                      <td className="px-4 py-2.5 font-mono">
                        <span className="text-[var(--color-text)]">{a.name}</span>
                        <span className="text-[var(--color-text-faint)]">@{a.version}</span>
                      </td>
                      <td className="px-3 py-2.5 text-[var(--color-text-muted)] font-mono text-[10px]">{a.asset_type}</td>
                      <td className="px-3 py-2.5">
                        <StatusBadge status={a.status} />
                      </td>
                      <td
                        className="px-3 py-2.5"
                        ref={(el) => rowRefCallback(el, a.id)}
                      >
                        <EvalSparkline history={sparklineHistory[a.id] ?? []} maxBars={10} />
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <div className="inline-flex items-center gap-1">
                          <button
                            onClick={() => {
                              onEval(a);
                              writeResumeItem({
                                tab: 'registry',
                                label: `Registry · ${a.name}@${a.version}`,
                                meta: `${a.status} · ${a.asset_type}`,
                                icon: '◈',
                              });
                            }}
                            title="Run eval suites"
                            className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-border-strong)] hover:text-[var(--color-text)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                          >eval</button>
                          <button
                            onClick={() => onPromote(a)}
                            title="Promote to next lifecycle stage"
                            className="px-2 py-1 rounded border border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                          >promote →</button>
                          {(versionsByName[a.name]?.length ?? 0) >= 2 && (
                            <button
                              onClick={() => onCompareOpen(a.name)}
                              title="Compare two versions of this asset"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-border-strong)] hover:text-[var(--color-text)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >compare…</button>
                          )}
                          {a.status === 'production' && (
                            <button
                              onClick={() => onRollback(a)}
                              title="Roll back to previous production version"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-status-failure)] hover:text-[var(--color-status-failure)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >rollback</button>
                          )}
                          {(a.status === 'staging' || a.status === 'pending_approval') && (
                            <button
                              onClick={() => onApprovalOpen(a)}
                              title="View and record approvals"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >approve…</button>
                          )}
                          {(a.status === 'development' || a.status === 'archived') && (
                            <button
                              onClick={() => onUnregister(a)}
                              title="Permanently remove this asset from the registry"
                              className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-status-failure)] hover:text-[var(--color-status-failure)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                            >delete</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Honest truncation + offset load-more (ADR-0199) — rendered even when
          a client-side filter empties the visible rows, matching the old
          always-present "Load more" row. */}
      <TruncationNotice
        shown={loadedCount}
        total={totalAssets}
        hasMore={hasMore}
        loadingMore={loadingMore}
        onLoadMore={loadMore}
      />


      {selectedIds.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-4 py-2 shadow-lg text-sm">
          <span className="text-[var(--color-text-muted)] text-xs">{selectedIds.size} selected</span>
          <button
            disabled={bulkBusy}
            onClick={onBulkPromote}
            className="px-3 py-1 rounded-full bg-[var(--color-accent)] text-[var(--color-accent-fg)] text-xs font-medium hover:opacity-90 disabled:opacity-50"
          >
            {bulkBusy ? 'Promoting…' : `Promote ${selectedIds.size}`}
          </button>
          <button
            onClick={() => setSelectedIds(new Set())}
            className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-xs"
          >Deselect all</button>
        </div>
      )}
    </>
  );
}
