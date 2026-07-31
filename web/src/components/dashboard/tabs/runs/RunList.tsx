/**
 * Virtualized run list: compare banners, per-row action buttons, and the
 * honest-truncation footer.
 *
 * Extracted verbatim from the former RunsTab monolith — behavior frozen —
 * except the hand-rolled "Load more" footer, which now renders through the
 * shared ADR-0199 `TruncationNotice` fed by the server's `total_approx` /
 * `next_cursor`. Rows are rich multi-line cards (status, command, cost,
 * two action rows), so the tab keeps its own `useVirtualizer` rather than
 * adopting the plain-table `DataTable`.
 */
import { useState, useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { clsx } from 'clsx';
import type { RunSummary } from '../../../../lib/api';
import { StatusDot } from '../../helpers';
import EmptyState from '../../../ui/EmptyState';
import TruncationNotice from '../../../ui/TruncationNotice';
import { writeResumeItem } from '../HomeTab';
import type { RunAction, RunCostEntry, ValidationState } from './types';
import { extractScenario } from './types';

export interface RunListProps {
  visibleRuns: RunSummary[];
  /** Total runs loaded client-side (pre-sort), for empty-state wording + Cmp gating. */
  loadedCount: number;
  totalApprox: number;
  selected: RunSummary | null;
  onSelect: (r: RunSummary) => void;
  checkedIds: string[];
  setCheckedIds: React.Dispatch<React.SetStateAction<string[]>>;
  compareA: string | null;
  setCompareA: (id: string | null) => void;
  onCompareTo?: (ids: string[]) => void;
  costMap: Record<string, RunCostEntry>;
  validationStates: Record<string, ValidationState>;
  onValidate: (runId: string) => void;
  onAction: (run: RunSummary, action: RunAction) => void;
  onShowSecrets: (run: RunSummary) => void;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
}

function ValidationErrorBadge({ errors }: { errors: string[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <span className="inline-flex flex-col gap-0.5">
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[var(--text-2xs)] font-mono font-medium border border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]"
      >
        ✗ {errors.length} error{errors.length !== 1 ? 's' : ''} {expanded ? '▲' : '▼'}
      </button>
      {expanded && (
        <ul className="mt-0.5 ml-0.5 space-y-0.5 max-w-[220px]">
          {errors.map((err, i) => (
            <li key={i} className="text-[var(--text-2xs)] font-mono text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_20%,transparent)] rounded px-1.5 py-0.5 break-all">
              {err}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

export default function RunList({
  visibleRuns,
  loadedCount,
  totalApprox,
  selected,
  onSelect,
  checkedIds,
  setCheckedIds,
  compareA,
  setCompareA,
  onCompareTo,
  costMap,
  validationStates,
  onValidate,
  onAction,
  onShowSecrets,
  hasMore,
  loadingMore,
  loadMore,
}: RunListProps) {
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Virtual scroll — BL-5: collapse DOM at 10K rows
  const rowVirtualizer = useVirtualizer({
    count: visibleRuns.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 40,
    overscan: 10,
  });

  return (
    <>
      {/* Compare selected banner — shown when 2–5 checkboxes are checked */}
      {checkedIds.length >= 2 && (
        <div className="px-3 py-2 flex items-center gap-2 border-b border-[var(--color-border)] bg-[color-mix(in_oklab,var(--color-accent)_8%,var(--color-bg-raised))] shrink-0">
          <span className="flex-1 text-[10px] font-mono text-[var(--color-text-muted)] truncate">
            {checkedIds.length} runs selected
          </span>
          <button
            type="button"
            onClick={() => {
              if (checkedIds.length >= 2 && onCompareTo) {
                onCompareTo(checkedIds);
                setCheckedIds([]);
              }
            }}
            className="shrink-0 px-2.5 py-1 text-[10px] rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] transition-colors"
          >
            Compare selected ⊕
          </button>
          <button
            type="button"
            onClick={() => setCheckedIds([])}
            title="Clear selection"
            className="shrink-0 text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-xs leading-none"
          >
            ×
          </button>
        </div>
      )}

      {compareA && (
        <div className="px-3 py-2 flex items-center gap-2 border-b border-[var(--color-border)] bg-[color-mix(in_oklab,var(--color-accent)_8%,var(--color-bg-raised))] text-[10px] font-mono text-[var(--color-text-muted)] shrink-0">
          <span className="flex-1 truncate">
            Run <code className="text-[var(--color-accent)]">{compareA.slice(0, 10)}…</code> selected as A — click another run&apos;s Cmp to compare
          </span>
          <button
            type="button"
            onClick={() => setCompareA(null)}
            title="Cancel selection"
            className="shrink-0 text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-xs leading-none"
          >
            ×
          </button>
        </div>
      )}

      {visibleRuns.length === 0 ? (
        loadedCount === 0
          ? <EmptyState
              message="No capsules yet."
              cliCommand="nova capture <cmd>"
              variant="inline"
            />
          : <EmptyState message="No runs match the current filter." variant="inline" />
      ) : (
        <div
          ref={listRef}
          className="overflow-y-auto flex-1"
          style={{ maxHeight: 600 }}
        >
          <div
            style={{ height: rowVirtualizer.getTotalSize(), position: 'relative' }}
          >
            {rowVirtualizer.getVirtualItems().map(virtualRow => {
              const r = visibleRuns[virtualRow.index];
              if (!r) return null;
              const isChecked = checkedIds.includes(r.run_id);
              const atLimit = checkedIds.length >= 5 && !isChecked;
              return (
            <div
              key={r.run_id}
              data-index={virtualRow.index}
              ref={rowVirtualizer.measureElement}
              style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${virtualRow.start}px)` }}
              className={clsx(
                'border-b border-[var(--color-border)]',
                selected?.run_id === r.run_id && 'bg-[color-mix(in_oklab,var(--color-accent)_6%,var(--color-bg-sunken))]',
              )}
            >
              <div className="flex items-start">
                {/* Checkbox column (36px wide) */}
                <div className="flex items-center justify-center w-9 shrink-0 pt-3">
                  <input
                    type="checkbox"
                    checked={isChecked}
                    disabled={atLimit}
                    title={atLimit ? 'Maximum 5 runs selected — deselect one first' : isChecked ? 'Deselect' : 'Select for comparison (up to 5)'}
                    onChange={() => {
                      if (isChecked) {
                        setCheckedIds(prev => prev.filter(id => id !== r.run_id));
                      } else if (checkedIds.length < 5) {
                        setCheckedIds(prev => [...prev, r.run_id]);
                      }
                    }}
                    className="w-3.5 h-3.5 rounded border border-[var(--color-border)] accent-[var(--color-accent)] cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed"
                  />
                </div>
                {/* Run content — stacks select button + action buttons vertically */}
                <div className="flex-1 flex flex-col min-w-0">
              <button
                onClick={() => {
                  onSelect(r);
                  writeResumeItem({
                    tab: 'runs',
                    label: `Inspecting run ${r.run_id.slice(0, 8)}`,
                    meta: `${r.command.join(' ').slice(0, 40)} · ${r.status ?? '—'}`,
                    icon: '🔍',
                  });
                }}
                className="text-left px-3 py-2.5 hover:bg-[var(--color-bg-sunken)] transition-colors"
              >
                {/* Row: status + id + duration */}
                <div className="flex items-center gap-2 mb-0.5">
                  <StatusDot status={r.status} />
                  <div className="relative group flex-1 min-w-0 flex items-center">
                    <code className="font-mono text-xs text-[var(--color-text)] truncate">{r.run_id.slice(0, 16)}…</code>
                    <button
                      type="button"
                      onClick={e => {
                        e.stopPropagation();
                        navigator.clipboard.writeText(r.run_id);
                        setCopiedId(r.run_id);
                        setTimeout(() => setCopiedId(null), 1500);
                      }}
                      title="Copy run ID"
                      className="opacity-0 group-hover:opacity-100 transition-opacity absolute right-0 top-1/2 -translate-y-1/2 text-[var(--color-text-faint)] hover:text-[var(--color-text)] px-0.5"
                    >
                      {copiedId === r.run_id ? (
                        <span className="text-[var(--text-2xs)] font-mono text-[var(--color-status-success)]">Copied</span>
                      ) : (
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3">
                          <path d="M5 3a1 1 0 000 2h6a1 1 0 100-2H5z"/>
                          <path fillRule="evenodd" d="M3 5a2 2 0 012-2h6a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V5zm2 1a1 1 0 000 2h6a1 1 0 100-2H5zm0 3a1 1 0 000 2h4a1 1 0 100-2H5z" clipRule="evenodd"/>
                        </svg>
                      )}
                    </button>
                  </div>
                  <span className="text-[10px] text-[var(--color-text-faint)] font-mono shrink-0 tabular-nums">
                    {r.duration_ms != null ? `${r.duration_ms}ms` : '—'}
                  </span>
                </div>
                {/* Command */}
                <p className="text-[10px] text-[var(--color-text-muted)] font-mono truncate" title={r.command.join(' ')}>
                  {r.command.join(' ')}
                </p>
                {extractScenario(r.command) && (
                  <span className="text-[var(--text-2xs)] font-mono px-1.5 py-px rounded bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)] truncate max-w-[140px]">
                    {extractScenario(r.command)}
                  </span>
                )}
                {/* Meta row */}
                <div className="flex items-center gap-3 mt-0.5 text-[var(--text-2xs)] text-[var(--color-text-faint)] font-mono">
                  <span>{r.created_at?.slice(0, 16) ?? '—'}</span>
                  <span>{r.model_call_count}m · {r.tool_call_count}t</span>
                  {r.exit_code !== null && r.exit_code !== 0 && (
                    <span className="text-[var(--color-status-failure)]">exit {r.exit_code}</span>
                  )}
                </div>
                {/* Cost row — only rendered when ClickHouse data is available */}
                {costMap[r.run_id] && (
                  <div className="flex items-center gap-3 mt-0.5 text-[var(--text-2xs)] text-[var(--color-text-faint)] font-mono">
                    <span title="Estimated LLM cost (ClickHouse)">
                      {costMap[r.run_id].cost_usd > 0
                        ? `$${costMap[r.run_id].cost_usd.toFixed(6)}`
                        : '—'}
                    </span>
                    <span title="Model API calls tracked in ClickHouse">
                      {costMap[r.run_id].calls > 0 ? `${costMap[r.run_id].calls} calls` : '—'}
                    </span>
                  </div>
                )}
              </button>
              {/* Action buttons */}
              <div className="px-3 pb-2 space-y-1">
                {/* Replay row: all inspection modes + compare + export */}
                <div className="flex items-center gap-1 flex-wrap">
                  <button
                    onClick={() => onAction(r, 'replay')}
                    title="Forensic replay (read-only inspection)"
                    className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                  >replay</button>
                  <button
                    onClick={() => onAction(r, 'dry-run')}
                    title="Dry-run policy check — which tools would be blocked?"
                    className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-status-pending)] hover:border-[var(--color-status-pending)] uppercase tracking-wider font-medium"
                  >dry-run</button>
                  <button
                    onClick={() => onAction(r, 'semantic')}
                    title="Semantic analysis — compute similarity score across model call responses"
                    className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-status-pending)] hover:border-[var(--color-status-pending)] uppercase tracking-wider font-medium"
                  >semantic</button>
                  <button
                    onClick={() => onAction(r, 'exact')}
                    title="Exact eligibility — check whether this capsule can be replayed byte-exactly"
                    className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                  >exact</button>
                  {(() => {
                    const isCompareA = compareA === r.run_id;
                    const canCompareVs = !!compareA && compareA !== r.run_id;
                    const buttonLabel = isCompareA ? 'A ×' : canCompareVs ? 'vs A' : 'Cmp';
                    const buttonTitle = isCompareA
                      ? 'Cancel selection'
                      : canCompareVs
                      ? `Compare against ${compareA!.slice(0, 8)}…`
                      : 'Select as baseline for comparison';
                    return (
                      <button
                        type="button"
                        title={buttonTitle}
                        onClick={() => {
                          if (isCompareA) { setCompareA(null); return; }
                          if (canCompareVs) { onCompareTo?.([compareA!, r.run_id]); setCompareA(null); return; }
                          setCompareA(r.run_id);
                        }}
                        disabled={!onCompareTo || loadedCount < 2}
                        className={clsx(
                          'px-1.5 py-0.5 rounded text-[10px] border transition-colors shrink-0',
                          isCompareA
                            ? 'border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                            : canCompareVs
                            ? 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]'
                            : 'border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]',
                          (!onCompareTo || loadedCount < 2) && 'opacity-40 cursor-not-allowed',
                        )}
                      >
                        {buttonLabel}
                      </button>
                    );
                  })()}
                  <button
                    onClick={() => onAction(r, 'export')}
                    title="Export signed evidence bundle"
                    className="ml-auto px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-accent)] hover:border-[var(--color-accent)] uppercase tracking-wider font-medium"
                  >export ↗</button>
                </div>
                {/* Data ops row: validate + redact + secrets */}
                {(() => {
                  const vs = validationStates[r.run_id];
                  return (
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <button
                        onClick={() => onValidate(r.run_id)}
                        disabled={vs?.loading === true}
                        title="Validate capsule schema and required files"
                        className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {vs?.loading ? 'validating…' : 'validate'}
                      </button>
                      {vs && !vs.loading && vs.result !== null && (
                        vs.result.valid ? (
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[var(--text-2xs)] font-mono font-medium border border-[color-mix(in_oklab,var(--color-status-success)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]">
                            ✓ valid
                          </span>
                        ) : (
                          <ValidationErrorBadge errors={vs.result.errors} />
                        )
                      )}
                      {vs && !vs.loading && vs.error !== null && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[var(--text-2xs)] font-mono font-medium border border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]" title={vs.error}>
                          ✗ error
                        </span>
                      )}
                      <button
                        onClick={() => onAction(r, 'redact')}
                        title="Re-scan & rewrite redaction-proof.json"
                        className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                      >redact</button>
                      <button
                        onClick={() => onShowSecrets(r)}
                        title="View secret scan / redaction proof"
                        className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] uppercase tracking-wider font-medium"
                      >secrets</button>
                      <button
                        onClick={() => onAction(r, 'delete')}
                        title="Permanently delete this capsule"
                        className="px-1.5 py-0.5 text-[var(--text-2xs)] rounded border border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] uppercase tracking-wider font-medium ml-auto"
                      >delete</button>
                    </div>
                  );
                })()}
              </div>
                </div>{/* end content column */}
              </div>{/* end flex items-start */}
            </div>
            );
            })}
          </div>
        </div>
      )}

      {/* Honest truncation + cursor load-more (B-1 / ADR-0199) */}
      <div className="shrink-0">
        <TruncationNotice
          shown={visibleRuns.length}
          total={totalApprox}
          approximate
          hasMore={hasMore}
          loadingMore={loadingMore}
          onLoadMore={loadMore}
        />
      </div>
    </>
  );
}
