/**
 * Runs list header: title + live indicator, search box, status chips, sort,
 * date range, and the E2 saved-views preset bar.
 * Extracted verbatim from the former RunsTab monolith — behavior frozen.
 */
import { clsx } from 'clsx';
import SavedViewsBar from '../../SavedViewsBar';
import type { Tab } from '../../Sidebar';
import type { StatusFilter, RunSort } from './types';

export interface RunFiltersProps {
  visibleCount: number;
  totalApprox: number;
  liveConnected: boolean;
  onNavigate?: (tab: Tab) => void;
  refresh: () => void;
  search: string;
  setSearch: (v: string) => void;
  statusFilter: StatusFilter;
  setStatusFilter: (v: StatusFilter) => void;
  sort: RunSort;
  setSort: (v: RunSort) => void;
  since: string;
  setSince: (v: string) => void;
  until: string;
  setUntil: (v: string) => void;
}

export default function RunFilters({
  visibleCount,
  totalApprox,
  liveConnected,
  onNavigate,
  refresh,
  search,
  setSearch,
  statusFilter,
  setStatusFilter,
  sort,
  setSort,
  since,
  setSince,
  until,
  setUntil,
}: RunFiltersProps) {
  return (
    <header className="px-3 py-2 border-b border-[var(--color-border)] space-y-2 shrink-0">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium text-[var(--color-text)]">
          Runs
          <span className="text-[var(--color-text-faint)] font-mono ml-1.5">
            ({visibleCount}{totalApprox > visibleCount ? ` of ~${totalApprox}` : ''})
          </span>
        </h3>
        <div className="flex items-center gap-2">
          {liveConnected && (
            <span
              title="Live stream connected — new runs appear automatically"
              className="flex items-center gap-1 text-[var(--text-2xs)] font-mono text-[var(--color-status-success)]"
            >
              <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-status-success)] animate-pulse inline-block" />
              live
            </span>
          )}
          {onNavigate && (
            <button
              onClick={() => onNavigate('commands')}
              className="text-[10px] text-[var(--color-accent)] hover:underline"
            >
              ▶ Capture new run
            </button>
          )}
          <button onClick={refresh} className="text-[10px] text-[var(--color-text-muted)] hover:text-[var(--color-text)]">↻ refresh</button>
        </div>
      </div>
      <input
        type="search"
        value={search}
        onChange={e => setSearch(e.target.value)}
        onKeyDown={e => e.key === 'Enter' && refresh()}
        placeholder="Search run_id or command… (Enter to search)"
        className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
      />
      <div className="flex gap-2 flex-wrap">
        {(['all', 'running', 'success', 'failure', 'error'] as StatusFilter[]).map(s => (
          <button
            key={s}
            type="button"
            onClick={() => { setStatusFilter(s); }}
            className={clsx(
              'px-2 py-0.5 rounded border text-[10px] uppercase tracking-wider font-medium transition-colors',
              statusFilter === s
                ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)] border-[var(--color-accent)]'
                : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
            )}
          >
            {s}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2 text-[10px]">
        <select value={sort} onChange={e => setSort(e.target.value as RunSort)}
          className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-1 font-mono">
          <option value="newest">newest first</option>
          <option value="oldest">oldest first</option>
          <option value="longest">longest first</option>
          <option value="shortest">shortest first</option>
        </select>
      </div>
      {/* Date filter */}
      <div className="flex gap-1.5 items-center flex-wrap text-[10px]">
        <label className="text-[var(--color-text-faint)]">From</label>
        <input
          type="date"
          value={since}
          onChange={e => setSince(e.target.value)}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1 py-0.5 font-mono"
        />
        <label className="text-[var(--color-text-faint)]">To</label>
        <input
          type="date"
          value={until}
          onChange={e => setUntil(e.target.value)}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1 py-0.5 font-mono"
        />
        {(since || until) && (
          <button
            onClick={() => { setSince(''); setUntil(''); }}
            className="px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] cursor-pointer"
          >
            Clear
          </button>
        )}
      </div>
      {/* E2 — saved views: persist the current filter set as a named preset */}
      <SavedViewsBar
        namespace="runs"
        current={{ search, statusFilter, sort, since, until }}
        onApply={(v) => {
          setSearch(v.search);
          setStatusFilter(v.statusFilter);
          setSort(v.sort);
          setSince(v.since);
          setUntil(v.until);
        }}
      />
    </header>
  );
}
