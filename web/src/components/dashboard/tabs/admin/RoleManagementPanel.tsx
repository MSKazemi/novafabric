// Role management (v0.27.0). Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { useState } from 'react';
import { api, type RolesListResult } from '../../../../lib/api';
import { Panel, SectionHeading } from './helpers';

const ROLES = ['reader', 'writer', 'admin', 'auditor'] as const;
type RoleValue = (typeof ROLES)[number];

export default function RoleManagementPanel({
  rolesData,
  onChanged,
}: {
  rolesData: RolesListResult | null;
  onChanged: () => void;
}) {
  const inputClass =
    'w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]';
  const labelClass = 'block text-[10px] font-mono text-[var(--color-text-faint)] mb-1';

  const [assignSubject, setAssignSubject] = useState('');
  const [assignRole, setAssignRole] = useState<RoleValue>('reader');
  const [assignMsg, setAssignMsg] = useState<string | null>(null);
  const [assignErr, setAssignErr] = useState<string | null>(null);
  const [assigning, setAssigning] = useState(false);

  const [revokeSubject, setRevokeSubject] = useState('');
  const [revokeRole, setRevokeRole] = useState<RoleValue>('reader');
  const [revokeMsg, setRevokeMsg] = useState<string | null>(null);
  const [revokeErr, setRevokeErr] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);

  const handleAssign = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!assignSubject.trim()) return;
    setAssigning(true);
    setAssignMsg(null);
    setAssignErr(null);
    try {
      const r = await api.assignRole(assignSubject.trim(), assignRole);
      setAssignMsg(`Assigned ${r.role} to ${r.subject}`);
      onChanged();
    } catch (err) {
      setAssignErr((err as Error).message);
    } finally {
      setAssigning(false);
    }
  };

  const handleRevoke = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!revokeSubject.trim()) return;
    setRevoking(true);
    setRevokeMsg(null);
    setRevokeErr(null);
    try {
      const r = await api.revokeRole(revokeSubject.trim(), revokeRole);
      setRevokeMsg(`Revoked ${r.role} from ${r.subject}`);
      onChanged();
    } catch (err) {
      setRevokeErr((err as Error).message);
    } finally {
      setRevoking(false);
    }
  };

  return (
    <Panel>
      <SectionHeading>Role management</SectionHeading>
      {!rolesData?.server_mode && (
        <div className="flex items-start gap-2 mb-4">
          <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-text-faint)] shrink-0 mt-1" aria-hidden="true" />
          <p className="text-xs text-[var(--color-text-muted)]">
            {rolesData?.message ||
              'Role management requires server mode with OIDC configured (NOVA_OIDC_ISSUER, NOVA_OIDC_CLIENT_ID). In local mode, assignments are stored but the shared token is unconditionally admin.'}
          </p>
        </div>
      )}
      {rolesData?.server_mode && (
        <div className="flex items-center gap-2 mb-4">
          <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-status-success)] shrink-0" aria-hidden="true" />
          <span className="text-xs text-[var(--color-status-success)]">OIDC configured — {rolesData.roles.length} role(s) assigned</span>
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {/* Assign */}
        <form onSubmit={handleAssign} className="space-y-3">
          <p className="text-xs font-medium text-[var(--color-text)]">Assign role</p>
          <div>
            <label className={labelClass}>Subject (user / token fingerprint)</label>
            <input
              value={assignSubject}
              onChange={(e) => setAssignSubject(e.target.value)}
              placeholder="user@example.com"
              className={inputClass}
            />
          </div>
          <div>
            <label className={labelClass}>Role</label>
            <select
              value={assignRole}
              onChange={(e) => setAssignRole(e.target.value as RoleValue)}
              className={inputClass}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={assigning || !assignSubject.trim()}
            className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white disabled:opacity-50 transition-colors"
          >
            {assigning ? 'Assigning…' : 'Assign'}
          </button>
          {assignMsg && <p className="text-xs text-[var(--color-status-success)] font-mono">{assignMsg}</p>}
          {assignErr && <p className="text-xs text-[var(--color-status-failure)] font-mono">{assignErr}</p>}
        </form>

        {/* Revoke */}
        <form onSubmit={handleRevoke} className="space-y-3">
          <p className="text-xs font-medium text-[var(--color-text)]">Revoke role</p>
          <div>
            <label className={labelClass}>Subject (user / token fingerprint)</label>
            <input
              value={revokeSubject}
              onChange={(e) => setRevokeSubject(e.target.value)}
              placeholder="user@example.com"
              className={inputClass}
            />
          </div>
          <div>
            <label className={labelClass}>Role</label>
            <select
              value={revokeRole}
              onChange={(e) => setRevokeRole(e.target.value as RoleValue)}
              className={inputClass}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={revoking || !revokeSubject.trim()}
            className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-status-failure)] text-[var(--color-status-failure)] hover:bg-[var(--color-status-failure)] hover:text-white disabled:opacity-50 transition-colors"
          >
            {revoking ? 'Revoking…' : 'Revoke'}
          </button>
          {revokeMsg && <p className="text-xs text-[var(--color-status-success)] font-mono">{revokeMsg}</p>}
          {revokeErr && <p className="text-xs text-[var(--color-status-failure)] font-mono">{revokeErr}</p>}
        </form>
      </div>
    </Panel>
  );
}
