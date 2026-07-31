// DD-7: Object Store Browser Card. Extracted verbatim from InfraTab.tsx
// (dashboard-modernization split).
import { useEffect, useState } from 'react';
import { api } from '../../../../lib/api';
import { BADGE_COLOR, StatRow } from './badges';

type StorageStats = Awaited<ReturnType<typeof api.storageStats>>;
type ManifestChain = Awaited<ReturnType<typeof api.storageManifestChain>>;

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export default function ObjectStoreCard() {
  const [stats, setStats] = useState<StorageStats | null>(null);
  const [chain, setChain] = useState<ManifestChain | null>(null);
  const [loading, setLoading] = useState(false);

  const doFetch = () => {
    setLoading(true);
    Promise.all([
      api.storageStats(),
      api.storageManifestChain(20),
    ])
      .then(([s, c]) => {
        setStats(s);
        setChain(c);
      })
      .catch(() => {/* silently ignore */})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    doFetch();
  }, []);

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">Object Capsule Store</h3>
            <span className={`text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
              live
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">Phase 4 — v0.12</div>
        </div>
        <button
          onClick={doFetch}
          disabled={loading}
          className="shrink-0 text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Content-addressable capsule storage with manifest chain (Iceberg-style), checkpoint compaction, NovaSeal write contract, and WORM backend adapters (S3/MinIO/Ceph/Azure/GCS) — ADR-0047, 0048, 0049.
      </p>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">Shipped</div>
        <ul className="space-y-0.5">
          {[
            'put_capsule p99 ≤ 350ms; 100K rebuild in 0.77s',
            'WORM conformance suite (10/10): nova-worm-test CLI + signed JSON attestation',
            'Local WAL + S3/MinIO/Ceph/Azure adapters; GCS stub',
            'Manifest chain rebuild test passes',
          ].map((s, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[var(--color-text-muted)]">
              <span className="shrink-0 mt-px text-[var(--color-status-success)]">✓</span>
              {s}
            </li>
          ))}
        </ul>
      </div>

      {/* Live storage stats */}
      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2.5 space-y-2">
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)]">Storage Stats</div>

        {stats === null ? (
          <p className="text-[11px] text-[var(--color-text-faint)]">Loading…</p>
        ) : !stats.configured ? (
          <div className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-[var(--color-status-pending)]" />
              <span className="text-[11px] text-[var(--color-status-pending)] font-medium">Object store not configured</span>
            </div>
            <p className="text-[11px] text-[var(--color-text-faint)]">
              Set one of these environment variables to enable:
            </p>
            <ul className="space-y-0.5 text-[11px] text-[var(--color-text-muted)]">
              <li><code className="font-mono text-[10px]">NOVA_OBJECT_STORE_PATH</code> — local WAL path</li>
              <li><code className="font-mono text-[10px]">NOVA_S3_BUCKET</code> — S3/MinIO/Ceph bucket</li>
            </ul>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
            <StatRow label="Backend" value={stats.backend_type} />
            <StatRow label="Chunks" value={stats.total_chunks != null ? stats.total_chunks.toLocaleString() : 'N/A'} />
            <StatRow label="Total size" value={stats.total_size_bytes != null ? formatBytes(stats.total_size_bytes) : 'N/A'} />
            <StatRow label="WORM score" value={stats.worm_score != null ? String(stats.worm_score) : 'N/A'} />
            <StatRow label="Chain head" value={stats.manifest_chain_head ?? 'N/A'} />
            <StatRow label="put_capsule p99" value={stats.last_put_p99_ms != null ? `${stats.last_put_p99_ms} ms` : 'N/A'} />
            {stats.error && (
              <div className="col-span-2 text-[10px] text-[var(--color-status-pending)]">{stats.error}</div>
            )}
          </div>
        )}
      </div>

      {/* Manifest chain browser */}
      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2.5 space-y-2">
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)]">Manifest Chain (last 20)</div>

        {chain === null ? (
          <p className="text-[11px] text-[var(--color-text-faint)]">Loading…</p>
        ) : chain.entries.length === 0 ? (
          <p className="text-[11px] text-[var(--color-text-faint)] italic">No manifest entries yet.</p>
        ) : (
          <div style={{ maxHeight: 200, overflowY: 'auto' }}>
            <table className="w-full text-[10px]">
              <thead>
                <tr className="text-left text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                  <th className="pb-1 pr-3 font-medium">Hash</th>
                  <th className="pb-1 pr-3 font-medium">Run ID</th>
                  <th className="pb-1 pr-3 font-medium">Timestamp</th>
                  <th className="pb-1 font-medium">Size</th>
                </tr>
              </thead>
              <tbody>
                {chain.entries.map((e, i) => (
                  <tr key={i} className="border-b border-[var(--color-border)] last:border-0">
                    <td className="py-1 pr-3 font-mono text-[var(--color-text-muted)]">{e.hash}</td>
                    <td className="py-1 pr-3 font-mono text-[var(--color-text-muted)] max-w-[120px] truncate">{e.run_id || '—'}</td>
                    <td className="py-1 pr-3 text-[var(--color-text-faint)]">{e.timestamp || '—'}</td>
                    <td className="py-1 text-[var(--color-text-faint)]">{formatBytes(e.size_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="text-[11px] text-[var(--color-text-faint)] italic leading-relaxed border-l-2 border-[var(--color-border)] pl-2">
        Object store is configured via environment variables and used transparently by nova capture.
      </p>
    </div>
  );
}
