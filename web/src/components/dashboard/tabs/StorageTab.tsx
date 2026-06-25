/**
 * Storage & Recovery — WORM storage, collector health, and DB recovery:
 *   nova storage inspect/validate   → object-store + S3 Object Lock checks
 *   nova collector status            → spool lag / signing p99
 *   nova db upgrade                  → alembic migration
 *   nova rebuild-metadata-db         → disaster-recovery rebuild (gated)
 *   nova export-system-card          → sealed system/audit card
 */
import { useEffect, useState } from 'react';
import { api } from '../../../lib/api';
import { useMutation } from '../../../lib/useMutation';
import { useUrlState } from '../../../lib/useUrlState';
import TabShell from './TabShell';
import ActionButton from '../../ui/ActionButton';
import DataTable, { type Column } from '../../ui/DataTable';

interface ChainEntry { hash: string; run_id: string; timestamp: string; size_bytes: number }

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
      <div className="text-sm font-mono text-[var(--color-text)] mt-0.5">{value}</div>
    </div>
  );
}

export default function StorageTab() {
  const [stats, setStats] = useState<Awaited<ReturnType<typeof api.storageStats>> | null>(null);
  const [collector, setCollector] = useState<Awaited<ReturnType<typeof api.collectorStatus>> | null>(null);
  const [chain, setChain] = useState<ChainEntry[]>([]);
  const [chainLoading, setChainLoading] = useState(true);
  const [chainError, setChainError] = useState<string | null>(null);
  const [runId, setRunId] = useUrlState('run_id', '');

  const load = () => {
    api.storageStats().then(setStats).catch(() => setStats(null));
    api.collectorStatus().then(setCollector).catch(() => setCollector(null));
    setChainLoading(true);
    setChainError(null);
    api.storageManifestChain(50)
      .then((r) => setChain(r.entries ?? []))
      .catch((e) => setChainError(e instanceof Error ? e.message : String(e)))
      .finally(() => setChainLoading(false));
  };
  useEffect(load, []);

  const validate = useMutation(() => api.storageValidate(), { silentSuccess: true });
  const inspect = useMutation((rid: string) => api.storageInspect(rid), { silentSuccess: true });
  const dbUpgrade = useMutation(() => api.dbUpgrade(), { successMessage: 'DB migrated to head' });
  const systemCard = useMutation((rid: string) => api.exportSystemCard(rid), { successMessage: 'System card sealed' });
  const rebuild = useMutation(() => api.rebuildMetadataDb(), {
    successMessage: (r) => `Rebuilt — ${r.capsules_found ?? 0} capsules`,
  });

  const chainCols: Column<ChainEntry>[] = [
    { key: 'hash', header: 'Hash', className: 'w-44', render: (r) => <span className="font-mono text-[10px] truncate">{r.hash}</span> },
    { key: 'run_id', header: 'Run', className: 'flex-1', render: (r) => <span className="font-mono text-[10px] truncate">{r.run_id}</span> },
    { key: 'timestamp', header: 'Timestamp', className: 'w-44', sortValue: (r) => r.timestamp, render: (r) => <span className="text-[10px]">{r.timestamp}</span> },
    { key: 'size_bytes', header: 'Size', className: 'w-24', align: 'right', sortValue: (r) => r.size_bytes, render: (r) => `${(r.size_bytes / 1024).toFixed(1)} KB` },
  ];

  const rid = runId.trim();

  return (
    <TabShell
      title="Storage & Recovery"
      subtitle="WORM object store, collector health, and metadata-DB recovery."
      cli={['nova storage inspect', 'nova storage validate', 'nova collector status', 'nova db upgrade', 'nova rebuild-metadata-db', 'nova export-system-card']}
      help="WORM stats and the manifest chain are read-only. Destructive recovery (rebuild-metadata-db, db upgrade) is confirmation-gated per the safe-mutations policy."
      actions={<ActionButton onClick={load} variant="ghost" size="sm">Refresh</ActionButton>}
    >
      {/* Storage stats */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Object store (WORM)</h3>
        {stats ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Stat label="Backend" value={stats.backend_type || '—'} />
            <Stat label="Chunks" value={stats.total_chunks ?? '—'} />
            <Stat label="WORM score" value={stats.worm_score ?? '—'} />
            <Stat label="Put p99" value={stats.last_put_p99_ms != null ? `${stats.last_put_p99_ms} ms` : '—'} />
          </div>
        ) : (
          <p className="text-xs text-[var(--color-text-faint)]">Object store not configured.</p>
        )}
        <div className="flex flex-wrap items-end gap-2 pt-1">
          <ActionButton onClick={() => validate.run()} pending={validate.pending}>Validate WORM setup</ActionButton>
          {validate.result && (
            <span className={validate.result.ok ? 'text-xs text-[var(--color-status-success)]' : 'text-xs text-[var(--color-status-failure)]'}>
              {validate.result.ok ? '✓ valid' : validate.result.error ?? 'invalid'}
            </span>
          )}
        </div>
      </section>

      {/* Collector */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Collector</h3>
        {collector?.detected ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Stat label="Spool lag" value={collector.spool_lag ?? '—'} />
            <Stat label="Sign p99" value={collector.signing_p99_ms != null ? `${collector.signing_p99_ms} ms` : '—'} />
            <Stat label="Events/s" value={collector.events_per_sec ?? '—'} />
            <Stat label="Version" value={collector.collector_version ?? '—'} />
          </div>
        ) : (
          <p className="text-xs text-[var(--color-text-faint)]">No collector detected (local mode).</p>
        )}
      </section>

      {/* Manifest chain */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Manifest chain</h3>
        <DataTable
          columns={chainCols}
          rows={chain}
          rowKey={(r) => r.hash}
          loading={chainLoading}
          error={chainError}
          onRetry={load}
          maxHeight={260}
        />
      </section>

      {/* Per-run inspect + system card */}
      <section className="rounded border border-[var(--color-border)] p-4 space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Per-run</h3>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
            Run ID
            <input
              value={runId}
              onChange={(e) => setRunId(e.target.value)}
              placeholder="01JABC…"
              className="w-64 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
            />
          </label>
          <ActionButton onClick={() => inspect.run(rid)} pending={inspect.pending} disabled={!rid}>Inspect storage</ActionButton>
          <ActionButton onClick={() => systemCard.run(rid)} pending={systemCard.pending} disabled={!rid} variant="primary">Export system card</ActionButton>
        </div>
        {inspect.result && (
          <pre className="whitespace-pre-wrap font-mono text-[10px] text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] rounded p-2 max-h-48 overflow-auto">{JSON.stringify(inspect.result, null, 2)}</pre>
        )}
      </section>

      {/* Recovery — gated */}
      <section className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] p-4 space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-status-failure)] uppercase tracking-wider">Recovery (destructive)</h3>
        <p className="text-[10px] text-[var(--color-text-faint)]">Global-scope operations. Confirmation required.</p>
        <div className="flex flex-wrap items-center gap-2">
          <ActionButton
            onClick={() => dbUpgrade.run()}
            pending={dbUpgrade.pending}
            variant="danger"
            confirm={{ title: 'Run DB migration?', body: 'Applies alembic migrations to the active metadata backend.', confirmLabel: 'Upgrade', tone: 'danger' }}
          >
            DB upgrade → head
          </ActionButton>
          <ActionButton
            onClick={() => rebuild.run()}
            pending={rebuild.pending}
            variant="danger"
            confirm={{ title: 'Rebuild metadata DB?', body: 'Replays the manifest chain log to reconstruct the metadata DB. Global scope; can take minutes.', confirmLabel: 'Rebuild', tone: 'danger' }}
          >
            Rebuild metadata DB
          </ActionButton>
          {rebuild.result?.ok && (
            <span className="text-xs text-[var(--color-status-success)] font-mono">
              {rebuild.result.capsules_found} capsules in {rebuild.result.time_to_replay_seconds?.toFixed(1)}s
            </span>
          )}
        </div>
      </section>
    </TabShell>
  );
}
