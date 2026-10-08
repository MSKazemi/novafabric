/**
 * Runs saved views — ADR-0232 D4 (experimental): "a saved view is the URL,
 * promoted" into an ADR-0130 `nova view`.
 *
 * When the server offers `/api/views`, saving writes a real NovaFabric saved
 * view (`.novafabric/views/<id>.yaml`) that `nova view show|run` reads, and the
 * list shows every view the Runs view can reproduce — including ones saved
 * from the CLI. A view whose query has no filter-bar form is listed disabled,
 * with the reason. Free-text search is not saved: it is not expressible in
 * `nova query`.
 *
 * When `/api/views` is unavailable (an older server, or no write access) the
 * bar falls back to browser-local views (`localStorage`, E2) and says so.
 * Views saved locally before this existed stay visible, marked *local*.
 */
import { useCallback, useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { api, type RunsViewState, type ServerSavedView } from '../../../../lib/api';
import { useSavedViews } from '../../../../lib/savedViews';
import type { RunSort, StatusFilter } from './types';

/** The shape the pre-D4 local views stored (E2). */
export interface LocalRunsView {
  search: string;
  statusFilter: StatusFilter;
  sort: RunSort;
  since: string;
  until: string;
  filter?: string;
  scope?: string;
}

type ServerState =
  | { kind: 'loading' }
  | { kind: 'ok'; views: ServerSavedView[] }
  | { kind: 'unavailable'; reason: string };

const pill = 'inline-flex items-center rounded border border-[var(--color-border)] overflow-hidden text-[10px]';

export default function RunsSavedViewsBar({
  current,
  onApply,
}: {
  current: LocalRunsView;
  onApply: (v: LocalRunsView) => void;
}) {
  const local = useSavedViews<LocalRunsView>('runs');
  const [server, setServer] = useState<ServerState>({ kind: 'loading' });
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: 'error' | 'info'; text: string } | null>(null);

  const reload = useCallback(() => {
    if (typeof api.listSavedViews !== 'function') {
      setServer({ kind: 'unavailable', reason: 'this dashboard build has no server views' });
      return;
    }
    api.listSavedViews()
      .then(r => setServer({ kind: 'ok', views: r.views }))
      .catch(e => setServer({ kind: 'unavailable', reason: (e as Error).message }));
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const state: RunsViewState = {
    f: current.filter ?? '',
    scope: (current.scope === 'root' || current.scope === 'tree') ? current.scope : 'node',
    since: current.since,
    until: current.until,
    status: current.statusFilter,
    sort: current.sort,
  };

  const save = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setMessage(null);
    if (server.kind !== 'ok') {
      local.save(trimmed, current);
      setName('');
      return;
    }
    setBusy(true);
    try {
      const exists = server.views.some(v => v.name === trimmed);
      await api.saveView(trimmed, state, exists);
      setName('');
      setMessage({
        tone: 'info',
        text: current.search.trim()
          ? 'Saved as a nova view — the search text is not part of a saved view (nova query cannot express it).'
          : 'Saved as a nova view.',
      });
      reload();
    } catch (e) {
      setMessage({ tone: 'error', text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const applyServer = (v: ServerSavedView) => {
    if (!v.dashboard) return;
    onApply({
      search: '',
      statusFilter: (v.dashboard.status as StatusFilter) ?? 'all',
      sort: v.dashboard.sort,
      since: v.dashboard.since,
      until: v.dashboard.until,
      filter: v.dashboard.f,
      scope: v.dashboard.scope,
    });
  };

  const removeServer = async (v: ServerSavedView) => {
    setMessage(null);
    try {
      await api.deleteView(v.view_id);
      reload();
    } catch (e) {
      setMessage({ tone: 'error', text: (e as Error).message });
    }
  };

  return (
    <div className="space-y-1" data-testid="runs-saved-views">
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Views:
        </span>
        {server.kind === 'ok' && server.views.map(v => (
          <span key={`s:${v.view_id}`} className={pill} data-testid="server-view">
            <button
              type="button"
              onClick={() => applyServer(v)}
              disabled={!v.dashboard}
              title={v.dashboard
                ? `Apply nova view "${v.view_id}" — ${v.cli_equivalent}`
                : (v.dashboard_unavailable_reason ?? 'not expressible in the Runs view')}
              className="px-2 py-0.5 font-mono text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-40 disabled:cursor-not-allowed"
            >{v.name}</button>
            <button
              type="button"
              onClick={() => removeServer(v)}
              title={`Delete nova view "${v.view_id}" (nova view rm)`}
              aria-label={`Delete view ${v.name}`}
              className="px-1.5 py-0.5 text-[var(--color-text-faint)] hover:text-[var(--color-status-failure)] border-l border-[var(--color-border)]"
            >×</button>
          </span>
        ))}
        {local.views.map(v => (
          <span key={`l:${v.name}`} className={pill} data-testid="local-view">
            <button
              type="button"
              onClick={() => onApply(v.value)}
              title={`Apply browser-local view "${v.name}"`}
              className="px-2 py-0.5 font-mono text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]"
            >{v.name}<span className="ml-1 text-[var(--color-text-faint)]">local</span></button>
            <button
              type="button"
              onClick={() => local.remove(v.name)}
              aria-label={`Delete local view ${v.name}`}
              className="px-1.5 py-0.5 text-[var(--color-text-faint)] hover:text-[var(--color-status-failure)] border-l border-[var(--color-border)]"
            >×</button>
          </span>
        ))}
        {(server.kind !== 'ok' || server.views.length === 0) && local.views.length === 0 && (
          <span className="text-[10px] text-[var(--color-text-faint)] italic">none saved</span>
        )}
        <input
          value={name}
          onChange={e => setName(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && void save()}
          placeholder="save current as…"
          aria-label="Saved view name"
          className="text-[10px] font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-0.5 w-32 focus:border-[var(--color-accent)] focus:outline-none"
        />
        <button
          type="button"
          onClick={() => void save()}
          disabled={!name.trim() || busy}
          title={server.kind === 'ok' ? 'Save as a NovaFabric saved view (nova view)' : 'Save in this browser'}
          className={clsx(
            'text-[10px] font-mono px-2 py-0.5 rounded border transition-colors',
            name.trim() && !busy
              ? 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-[var(--color-accent-fg)]'
              : 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed',
          )}
        >Save</button>
      </div>
      {server.kind === 'unavailable' && (
        <p className="text-[10px] text-[var(--color-text-faint)]" data-testid="views-local-fallback">
          Saving in this browser only — server saved views unavailable ({server.reason}).
        </p>
      )}
      {message && (
        <p
          role={message.tone === 'error' ? 'alert' : 'status'}
          className={clsx('text-[10px]', message.tone === 'error' ? 'text-[var(--color-status-failure)]' : 'text-[var(--color-text-muted)]')}
        >{message.text}</p>
      )}
    </div>
  );
}
