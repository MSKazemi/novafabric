// Entity review queue (nova kg entity-queue, Tier-3 human review). Extracted
// verbatim from KGTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { api } from '../../../../lib/api';
import EmptyState from '../../../ui/EmptyState';
import { inputClass } from './shared';

export default function EntityQueuePanel() {
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState<{ ok: boolean; pending: number; approved: number; rejected: number } | null>(null);
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]);
  const [approveInputs, setApproveInputs] = useState<Record<string, string>>({});
  const [actionResult, setActionResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setActionResult(null);
    try {
      const [statsR, listR] = await Promise.all([api.kgEntityQueueStats(), api.kgEntityQueueList()]);
      setStats(statsR);
      setItems(listR.items);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleApprove = useCallback(async (itemId: string) => {
    const canonical = approveInputs[itemId]?.trim();
    if (!canonical) return;
    try {
      await api.kgEntityQueueApprove(itemId, canonical);
      setActionResult(`Approved ${itemId}`);
      load();
    } catch (e) {
      setActionResult(`Error: ${(e as Error).message}`);
    }
  }, [approveInputs, load]);

  const handleReject = useCallback(async (itemId: string) => {
    try {
      await api.kgEntityQueueReject(itemId);
      setActionResult(`Rejected ${itemId}`);
      load();
    } catch (e) {
      setActionResult(`Error: ${(e as Error).message}`);
    }
  }, [load]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Entity Review Queue</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            nova kg entity-queue list/approve/reject/stats — Tier-3 human review for ambiguous aliases
          </p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] transition-colors"
        >
          {loading ? '…' : 'Refresh'}
        </button>
      </div>
      {stats && (
        <div className="flex gap-3 text-[10px] font-mono">
          <span className="text-[var(--color-status-pending)]">pending {stats.pending}</span>
          <span className="text-[var(--color-status-success)]">approved {stats.approved}</span>
          <span className="text-[var(--color-status-failure)]">rejected {stats.rejected}</span>
        </div>
      )}
      {actionResult && (
        <p className="text-[10px] font-mono text-[var(--color-text-muted)]">{actionResult}</p>
      )}
      {!stats && !loading && (
        <p className="text-xs text-[var(--color-text-faint)]">Click Refresh to load queue.</p>
      )}
      {items.length > 0 && (
        <div className="space-y-2 max-h-64 overflow-y-auto">
          {items.map((item) => {
            const id = String(item.item_id ?? '');
            return (
              <div key={id} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-xs space-y-1">
                <div className="font-mono text-[var(--color-text-muted)]">{id}</div>
                <div>Alias: <span className="font-mono">{String(item.alias ?? '')}</span> | Type: {String(item.entity_type ?? '')}</div>
                <div className="text-[10px] text-[var(--color-text-faint)]">Suggested: {String(item.suggested_canonical ?? '—')}</div>
                <div className="flex gap-2 items-center mt-1">
                  <input
                    type="text"
                    placeholder="canonical to approve"
                    value={approveInputs[id] ?? ''}
                    onChange={(e) => setApproveInputs((prev) => ({ ...prev, [id]: e.target.value }))}
                    className={`flex-1 ${inputClass}`}
                  />
                  <button
                    onClick={() => handleApprove(id)}
                    className="text-[10px] font-mono px-2 py-1 rounded border border-[color-mix(in_oklab,var(--color-status-success)_50%,transparent)] text-[var(--color-status-success)] hover:bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] transition-colors"
                  >Approve</button>
                  <button
                    onClick={() => handleReject(id)}
                    className="text-[10px] font-mono px-2 py-1 rounded border border-[color-mix(in_oklab,var(--color-status-failure)_50%,transparent)] text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] transition-colors"
                  >Reject</button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {stats && items.length === 0 && (
        <EmptyState message="No pending items in the entity review queue." />
      )}
    </section>
  );
}
