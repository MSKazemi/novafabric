// P7: Backup-Set Status Card. Extracted verbatim from InfraTab.tsx
// (dashboard-modernization split).
import { useEffect, useState } from 'react';
import { api } from '../../../../lib/api';
import { usePolling } from '../../../../lib/usePolling';
import { BADGE_COLOR } from './badges';

type BackupData = Awaited<ReturnType<typeof api.backupStatus>>;

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return 'N/A';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

export default function BackupCard() {
  const [data, setData] = useState<BackupData | null>(null);
  const [loading, setLoading] = useState(false);

  const doFetch = () => {
    setLoading(true);
    api.backupStatus()
      .then(setData)
      .catch(() => {/* endpoint may be unavailable — degrade silently */})
      .finally(() => setLoading(false));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { doFetch(); }, []);
  usePolling(doFetch, 15000);

  const backups = data?.backups ?? [];

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">Backup Sets</h3>
            <span className={`text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
              read-only
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">NOVA_BACKUP_DIR · manifest-claimed, not verified</div>
        </div>
        <button
          onClick={doFetch}
          disabled={loading}
          className="shrink-0 text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50"
        >
          {loading ? 'Checking…' : 'Refresh'}
        </button>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Lists <code className="font-mono">nova-backup-*.tar.gz</code> archives from the server&apos;s
        configured backup directory. This is a listing only — run{' '}
        <code className="font-mono">nova backup verify</code> to hash-check members and validate signatures (ADR-0181).
      </p>

      {data == null ? (
        <p className="text-[11px] text-[var(--color-text-faint)]">Checking backups…</p>
      ) : !data.detected ? (
        <div className="rounded border border-dashed border-[var(--color-border)] px-3 py-2 space-y-1">
          <span className="text-[11px] text-[var(--color-status-pending)] font-medium">Backup directory not configured</span>
          <p className="text-[10px] text-[var(--color-text-faint)]">
            {data.reason ?? 'Set NOVA_BACKUP_DIR to a directory of backup archives.'}
          </p>
        </div>
      ) : backups.length === 0 ? (
        <p className="text-[11px] text-[var(--color-text-faint)]">
          No backup archives found in <code className="font-mono">{data.directory}</code>.
        </p>
      ) : (
        <div className="space-y-2">
          <div className="text-[10px] text-[var(--color-text-faint)] font-mono">
            {data.count} set{data.count === 1 ? '' : 's'}{data.truncated ? ' (truncated)' : ''} · {data.directory}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-mono">
              <thead>
                <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                  <th className="text-left px-2 py-1">set</th>
                  <th className="text-left px-2 py-1">created</th>
                  <th className="text-left px-2 py-1">profile</th>
                  <th className="text-right px-2 py-1">members</th>
                  <th className="text-right px-2 py-1">size</th>
                  <th className="text-left px-2 py-1">signing</th>
                </tr>
              </thead>
              <tbody>
                {backups.map((b) => (
                  <tr key={b.filename} className="border-b border-[var(--color-border)] last:border-0">
                    {b.ok ? (
                      <>
                        <td className="px-2 py-1 text-[var(--color-text)] truncate max-w-[10rem]" title={b.set_id ?? ''}>{b.set_id}</td>
                        <td className="px-2 py-1 text-[var(--color-text-muted)]">{b.created_at}</td>
                        <td className="px-2 py-1 text-[var(--color-text-muted)]">{b.profile}</td>
                        <td className="px-2 py-1 text-right text-[var(--color-text-muted)]">{b.member_count}</td>
                        <td className="px-2 py-1 text-right text-[var(--color-text-muted)]">{fmtBytes(b.archive_bytes)}</td>
                        <td className="px-2 py-1">
                          <span className={b.signing_status === 'signed' ? 'text-[var(--color-status-success)]' : 'text-[var(--color-text-faint)]'}>
                            {b.signing_status}
                          </span>
                        </td>
                      </>
                    ) : (
                      <td colSpan={6} className="px-2 py-1 text-[var(--color-status-failure)] truncate" title={b.error ?? ''}>
                        {b.filename}: {b.error}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
