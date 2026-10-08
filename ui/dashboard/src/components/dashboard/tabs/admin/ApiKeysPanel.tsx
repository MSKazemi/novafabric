// API keys read-only view (ADR-0193). Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { useCallback, useEffect, useState } from 'react';
import { api, type ApiKeyRow } from '../../../../lib/api';
import { Panel, SectionHeading } from './helpers';

function ApiKeyStatusBadge({ status }: { status: ApiKeyRow['status'] }) {
  const cls =
    status === 'active'
      ? 'text-[var(--color-status-success)]'
      : status === 'expired'
        ? 'text-[var(--color-status-pending)]'
        : 'text-[var(--color-text-faint)]';
  return <span className={`font-mono text-[10px] ${cls}`}>{status}</span>;
}

export default function ApiKeysPanel() {
  const [keys, setKeys] = useState<ApiKeyRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const r = await api.adminApiKeys();
      setKeys(r.keys);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Panel>
      <SectionHeading>API keys</SectionHeading>
      <p className="text-xs text-[var(--color-text-muted)] mb-3">
        Read-only view of server API keys (ADR-0193). Keys are created, rotated, and revoked with{' '}
        <code className="font-mono text-[10px]">nova server api-key</code> or the{' '}
        <code className="font-mono text-[10px]">/v0/api-keys</code> resource — secrets are shown once
        at creation and never displayed here.
      </p>
      {err && <p className="text-[11px] font-mono text-[var(--color-status-failure)]">{err}</p>}
      {keys && keys.length === 0 && (
        <p className="text-xs text-[var(--color-text-faint)] py-2">
          No API keys. Create one with <code className="font-mono text-[10px]">nova server api-key create</code>.
        </p>
      )}
      {keys && keys.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
                <th className="px-3 py-2 text-left font-medium">Key ID</th>
                <th className="px-3 py-2 text-left font-medium">Owner</th>
                <th className="px-3 py-2 text-left font-medium">Roles</th>
                <th className="px-3 py-2 text-left font-medium">Workspace</th>
                <th className="px-3 py-2 text-left font-medium">Last used</th>
                <th className="px-3 py-2 text-left font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.key_id} className="border-b border-[var(--color-border)] last:border-b-0">
                  <td className="px-3 py-1.5 font-mono text-[11px] text-[var(--color-text)]">{k.key_id}</td>
                  <td className="px-3 py-1.5 text-[var(--color-text-muted)]">{k.owner}</td>
                  <td className="px-3 py-1.5 font-mono text-[10px] text-[var(--color-text-muted)]">{k.roles.join(', ')}</td>
                  <td className="px-3 py-1.5 text-[var(--color-text-faint)]">{k.workspace ?? '—'}</td>
                  <td className="px-3 py-1.5 font-mono text-[10px] text-[var(--color-text-faint)]">{k.last_used_at ?? 'never'}</td>
                  <td className="px-3 py-1.5"><ApiKeyStatusBadge status={k.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
