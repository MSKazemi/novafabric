import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import { ErrorBox, Loading } from '../helpers';
import EmptyState from '../../ui/EmptyState';

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
  const [entries, setEntries] = useState<Array<Record<string, unknown>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [resultFilter, setResultFilter] = useState<'all' | 'ok' | 'error' | 'orphaned'>('all');
  const [actionFilter, setActionFilter] = useState('all');

  const onCountChangeRef = useRef(onCountChange);
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  const refresh = useCallback(() => {
    setError(null);
    setActionFilter('all');
    api.audit(200)
      .then((r) => {
        setEntries(r.entries);
        onCountChangeRef.current?.(r.count);
      })
      .catch((e) => setError((e as Error).message));
  }, []); // onCountChange intentionally excluded — accessed via ref

  useEffect(() => { refresh(); }, [refresh, refreshTick]);

  const availableActions = useMemo(
    () => ['all', ...Array.from(new Set((entries ?? []).map(e => String(e.action ?? '')).filter(Boolean)))],
    [entries],
  );

  if (error && !entries) return <ErrorBox message={error} onRetry={refresh} />;
  if (!entries) return <Loading />;

  // Collect run_ids that have a matching done or interrupted event (over all entries, not just visible)
  const completedRunIds = new Set<string>();
  for (const e of entries) {
    const action = String(e.action ?? '');
    if (action.endsWith('_mission_done') || action.endsWith('_mission_interrupted')) {
      const args = isKVObject(e.args) ? e.args : null;
      if (typeof args?.run_id === 'string') completedRunIds.add(args.run_id);
    }
  }

  const visibleEntries = entries.filter(e => {
    const result = String(e.result ?? '?');
    const action = String(e.action ?? '');
    const eArgs = isKVObject(e.args) ? e.args : null;
    const runId = typeof eArgs?.run_id === 'string' ? eArgs.run_id : null;
    const isOrphaned = action.endsWith('_mission_start') && runId !== null && !completedRunIds.has(runId);
    if (actionFilter !== 'all' && action !== actionFilter) return false;
    if (resultFilter === 'ok' && result !== 'ok') return false;
    if (resultFilter === 'error' && result === 'ok') return false;
    if (resultFilter === 'orphaned' && !isOrphaned) return false;
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return action.toLowerCase().includes(q) || String(e.cli_equivalent ?? '').toLowerCase().includes(q);
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-xs">
        <p className="text-[var(--color-text-muted)]">
          {visibleEntries.length}{visibleEntries.length !== entries.length ? ` / ${entries.length}` : ''} audit entr{visibleEntries.length === 1 ? 'y' : 'ies'} · newest first ·
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

      {entries.length === 0 ? (
        <EmptyState
          icon="◎"
          message="No audit entries yet."
          hint="Mutations from the dashboard (register, eval, evidence export) will appear here."
        />
      ) : (
        <ul className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] divide-y divide-[var(--color-border)] overflow-hidden">
          {visibleEntries.length === 0 ? (
            <li><EmptyState message="No entries match the current filter." variant="inline" /></li>
          ) : visibleEntries.map((e, i) => {
            const result = String(e.result ?? '?');
            const okay = result === 'ok';
            const isMissionStart = String(e.action ?? '').endsWith('_mission_start');
            const eArgs = isKVObject(e.args) ? e.args : null;
            const eExtra = isKVObject(e.extra) ? e.extra : null;
            const runId = typeof eArgs?.run_id === 'string' ? eArgs.run_id : null;
            const isOrphaned = isMissionStart && runId !== null && !completedRunIds.has(runId);

            const dotColor = isOrphaned
              ? 'bg-amber-400'
              : okay
              ? 'bg-[var(--color-status-success)]'
              : 'bg-[var(--color-status-failure)]';

            return (
              <li key={(e.audit_id as string) ?? i} className="px-4 py-3 flex items-start gap-3">
                <span
                  className={clsx('mt-1.5 w-2 h-2 rounded-full shrink-0', dotColor)}
                  aria-hidden="true"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <code className="font-mono text-sm text-[var(--color-text)]">{String(e.action ?? '?')}</code>
                    {!okay && (
                      <span className="border border-[var(--color-status-failure)] text-[var(--color-status-failure)] px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider">
                        {result}
                      </span>
                    )}
                    {isOrphaned && (
                      <span className="border border-amber-500 text-amber-500 px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider">
                        never completed
                      </span>
                    )}
                    <span className="text-[10px] text-[var(--color-text-faint)] font-mono">{String(e.ts ?? '')}</span>
                  </div>
                  <p className="text-xs text-[var(--color-text-muted)] mt-0.5 font-mono break-all">
                    $ {String(e.cli_equivalent ?? '')}
                  </p>
                  {!!e.error && (
                    <p className="text-xs text-[var(--color-status-failure)] mt-1 font-mono break-all">
                      {String(e.error)}
                    </p>
                  )}
                  {eArgs && <KVList data={eArgs} label="args" />}
                  {eExtra && <KVList data={eExtra} label="extra" />}
                </div>
                <code className="text-[10px] text-[var(--color-text-faint)] font-mono shrink-0">
                  fp:{String(e.actor_token_fp ?? '')}
                </code>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
