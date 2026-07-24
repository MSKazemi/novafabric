import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import DataTable, { type Column } from '../../ui/DataTable';
import EmptyState from '../../ui/EmptyState';

const PAGE_SIZE = 200;

type AuditEntry = Record<string, unknown>;
type KVData = Record<string, unknown>;

function isKVObject(v: unknown): v is KVData {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function renderValue(v: unknown): string {
  if (Array.isArray(v)) return v.map(String).join(', ');
  if (typeof v === 'object' && v !== null) return JSON.stringify(v);
  return String(v ?? '');
}

function KVList({ data, label }: { data: KVData; label: string }) {
  const kvEntries = Object.entries(data);
  if (kvEntries.length === 0) return null;
  return (
    <div className="mt-1.5">
      <span className="text-[10px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider">{label}</span>
      <dl className="mt-0.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
        {kvEntries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-[10px] font-mono text-[var(--color-text-faint)] shrink-0 whitespace-nowrap">{k}:</dt>
            <dd className="text-[10px] font-mono text-[var(--color-text-muted)] break-all min-w-0">{renderValue(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export default function AuditTab({
  refreshTick,
  onCountChange,
}: {
  refreshTick: number;
  onCountChange?: (n: number) => void;
}) {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [resultFilter, setResultFilter] = useState<'all' | 'ok' | 'error' | 'orphaned'>('all');
  const [actionFilter, setActionFilter] = useState('all');
  // Server-side filtering narrows `entries` to one action, so the dropdown
  // options are accumulated across fetches instead of derived per page.
  const [knownActions, setKnownActions] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const onCountChangeRef = useRef(onCountChange);
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  const mergeKnownActions = useCallback((page: AuditEntry[]) => {
    setKnownActions(prev => {
      const next = new Set(prev);
      for (const e of page) {
        const a = String(e.action ?? '');
        if (a) next.add(a);
      }
      return next.size === prev.length ? prev : Array.from(next).sort();
    });
  }, []);

  const refresh = useCallback(() => {
    setError(null);
    setNextCursor(null);
    setSelectedId(null);
    api.audit(PAGE_SIZE, { action: actionFilter !== 'all' ? actionFilter : undefined })
      .then((r) => {
        setEntries(r.entries);
        setNextCursor(r.next_cursor);
        mergeKnownActions(r.entries);
        onCountChangeRef.current?.(r.count);
      })
      .catch((e) => setError((e as Error).message));
  }, [actionFilter, mergeKnownActions]); // onCountChange intentionally excluded — accessed via ref

  useEffect(() => { refresh(); }, [refresh, refreshTick]);

  const loadMore = useCallback(() => {
    if (nextCursor == null || loadingMore) return;
    setLoadingMore(true);
    api.audit(PAGE_SIZE, { cursor: nextCursor, action: actionFilter !== 'all' ? actionFilter : undefined })
      .then((r) => {
        setEntries(prev => [...(prev ?? []), ...r.entries]);
        setNextCursor(r.next_cursor);
        mergeKnownActions(r.entries);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoadingMore(false));
  }, [nextCursor, loadingMore, actionFilter, mergeKnownActions]);

  const availableActions = useMemo(
    () => ['all', ...knownActions],
    [knownActions],
  );

  // Collect run_ids that have a matching done or interrupted event (over all loaded entries)
  const completedRunIds = useMemo(() => {
    const ids = new Set<string>();
    for (const e of entries ?? []) {
      const action = String(e.action ?? '');
      if (action.endsWith('_mission_done') || action.endsWith('_mission_interrupted')) {
        const args = isKVObject(e.args) ? e.args : null;
        if (typeof args?.run_id === 'string') ids.add(args.run_id);
      }
    }
    return ids;
  }, [entries]);

  const isOrphaned = useCallback((e: AuditEntry): boolean => {
    const action = String(e.action ?? '');
    const args = isKVObject(e.args) ? e.args : null;
    const runId = typeof args?.run_id === 'string' ? args.run_id : null;
    return action.endsWith('_mission_start') && runId !== null && !completedRunIds.has(runId);
  }, [completedRunIds]);

  const visibleEntries = useMemo(() => (entries ?? []).filter(e => {
    const result = String(e.result ?? '?');
    if (resultFilter === 'ok' && result !== 'ok') return false;
    if (resultFilter === 'error' && result === 'ok') return false;
    if (resultFilter === 'orphaned' && !isOrphaned(e)) return false;
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return String(e.action ?? '').toLowerCase().includes(q)
      || String(e.cli_equivalent ?? '').toLowerCase().includes(q);
  }), [entries, resultFilter, search, isOrphaned]);

  const selected = useMemo(
    () => visibleEntries.find(e => String(e.audit_id ?? '') === selectedId) ?? null,
    [visibleEntries, selectedId],
  );

  const columns = useMemo<Column<AuditEntry>[]>(() => [
    { key: 'action', header: 'Action', className: 'flex-1', sortValue: (e) => String(e.action ?? ''),
      render: (e) => {
        const result = String(e.result ?? '?');
        const okay = result === 'ok';
        const orphaned = isOrphaned(e);
        const dotColor = orphaned
          ? 'bg-amber-400'
          : okay
          ? 'bg-[var(--color-status-success)]'
          : 'bg-[var(--color-status-failure)]';
        return (
          <span className="flex items-center gap-2 min-w-0">
            <span className={clsx('w-2 h-2 rounded-full shrink-0', dotColor)} aria-hidden="true" />
            <code className="font-mono truncate text-[var(--color-text)]">{String(e.action ?? '?')}</code>
            {!okay && (
              <span className="border border-[var(--color-status-failure)] text-[var(--color-status-failure)] px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider shrink-0">
                {result}
              </span>
            )}
            {orphaned && (
              <span className="border border-amber-500 text-amber-500 px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider shrink-0">
                never completed
              </span>
            )}
          </span>
        );
      } },
    { key: 'cli_equivalent', header: 'CLI equivalent', className: 'flex-1', sortValue: (e) => String(e.cli_equivalent ?? ''),
      render: (e) => (
        <span className="font-mono text-[var(--color-text-muted)] truncate">$ {String(e.cli_equivalent ?? '')}</span>
      ) },
    { key: 'ts', header: 'Timestamp', className: 'w-48', sortValue: (e) => String(e.ts ?? ''),
      render: (e) => <span className="text-[10px] font-mono text-[var(--color-text-faint)] truncate">{String(e.ts ?? '')}</span> },
    { key: 'actor_token_fp', header: 'Actor', className: 'w-24',
      render: (e) => <code className="text-[10px] font-mono text-[var(--color-text-faint)]">fp:{String(e.actor_token_fp ?? '')}</code> },
  ], [isOrphaned]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-xs">
        <p className="text-[var(--color-text-muted)]">
          {visibleEntries.length}{entries && visibleEntries.length !== entries.length ? ` / ${entries.length}` : ''} audit entr{visibleEntries.length === 1 ? 'y' : 'ies'} · newest first ·
          <span className="font-mono ml-2 text-[10px] text-[var(--color-text-faint)]">
            file: ~/.novafabric/dashboard-audit.jsonl
          </span>
        </p>
        <button onClick={refresh} className="text-[var(--color-text-muted)] hover:text-[var(--color-text)]">
          refresh
        </button>
      </div>
      <div className="flex items-center gap-2">
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search action or CLI equivalent…"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <select
          value={actionFilter}
          onChange={e => setActionFilter(e.target.value)}
          className="px-2 py-1 rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-xs text-[var(--color-text-muted)] focus:border-[var(--color-accent)] focus:outline-none"
        >
          {availableActions.map(a => (
            <option key={a} value={a}>{a === 'all' ? 'All actions' : a}</option>
          ))}
        </select>
        <select
          value={resultFilter}
          onChange={e => setResultFilter(e.target.value as typeof resultFilter)}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-1.5 font-mono text-xs"
        >
          <option value="all">all results</option>
          <option value="ok">ok only</option>
          <option value="error">errors only</option>
          <option value="orphaned">orphaned runs</option>
        </select>
      </div>

      <DataTable<AuditEntry>
        columns={columns}
        rows={visibleEntries}
        rowKey={(e, i) => String(e.audit_id ?? i)}
        loading={!entries && !error}
        error={entries ? null : error}
        onRetry={refresh}
        onRowClick={(e) => {
          const id = String(e.audit_id ?? '');
          setSelectedId(prev => (prev === id ? null : id));
        }}
        empty={
          entries && entries.length === 0 ? (
            <EmptyState
              icon="◎"
              message="No audit entries yet."
              hint="Mutations from the dashboard (register, eval, evidence export) will appear here."
            />
          ) : (
            <EmptyState message="No entries match the current filter." variant="inline" />
          )
        }
      />

      {selected && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-4 py-3">
          <div className="flex items-baseline gap-2 flex-wrap">
            <code className="font-mono text-sm text-[var(--color-text)]">{String(selected.action ?? '?')}</code>
            <span className="text-[10px] text-[var(--color-text-faint)] font-mono">{String(selected.ts ?? '')}</span>
            <code className="text-[10px] text-[var(--color-text-faint)] font-mono ml-auto">
              fp:{String(selected.actor_token_fp ?? '')}
            </code>
          </div>
          <p className="text-xs text-[var(--color-text-muted)] mt-0.5 font-mono break-all">
            $ {String(selected.cli_equivalent ?? '')}
          </p>
          {!!selected.error && (
            <p className="text-xs text-[var(--color-status-failure)] mt-1 font-mono break-all">
              {String(selected.error)}
            </p>
          )}
          {isKVObject(selected.args) && <KVList data={selected.args} label="args" />}
          {isKVObject(selected.extra) && <KVList data={selected.extra} label="extra" />}
        </div>
      )}

      {error && entries && (
        <p className="text-xs text-[var(--color-status-failure)] font-mono">{error}</p>
      )}

      {/* Load more (byte-offset cursor pagination — ADR-0199) */}
      {nextCursor != null && (
        <button
          onClick={loadMore}
          disabled={loadingMore}
          className="w-full px-2 py-1.5 text-[10px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loadingMore ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  );
}
