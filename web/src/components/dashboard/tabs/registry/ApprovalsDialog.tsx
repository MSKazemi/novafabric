/**
 * DD-4 approvals modal: list recorded maker-checker approvals for an asset
 * and record a new one.
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 */
import type { AssetSummary } from '../../../../lib/api';
import type { ApprovalRecord } from './useRegistryActions';

export default function ApprovalsDialog({
  asset,
  approvals,
  required,
  loading,
  role,
  setRole,
  note,
  setNote,
  busy,
  onSubmit,
  onClose,
}: {
  asset: AssetSummary;
  approvals: ApprovalRecord[];
  required: number;
  loading: boolean;
  role: string;
  setRole: (v: string) => void;
  note: string;
  setNote: (v: string) => void;
  busy: boolean;
  onSubmit: () => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-lg rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-xl p-5 space-y-4">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-sm font-semibold text-[var(--color-text)]">
              Approvals — {asset.name}@{asset.version}
            </h2>
            <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
              CLI equivalent: <code className="font-mono">nova approve {asset.name}@{asset.version} --role &lt;role&gt;</code>
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none"
            aria-label="Close"
          >×</button>
        </div>

        {/* Approval count */}
        <div className="text-xs text-[var(--color-text-muted)]">
          {loading
            ? 'Loading approvals…'
            : `${approvals.length} approval${approvals.length === 1 ? '' : 's'} recorded / ${required} required`
          }
        </div>

        {/* Existing approvals list */}
        {!loading && approvals.length > 0 && (
          <div className="rounded border border-[var(--color-border)] overflow-hidden">
            <table className="w-full text-[10px] font-mono">
              <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)]">
                <tr>
                  <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Role</th>
                  <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Actor</th>
                  <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Note</th>
                  <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">At</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--color-border)]">
                {approvals.map((ap, i) => (
                  <tr key={i} className="hover:bg-[var(--color-bg-sunken)] transition-colors">
                    <td className="px-3 py-1.5 text-[var(--color-text-muted)] uppercase tracking-wider text-[var(--text-2xs)]">{ap.role}</td>
                    <td className="px-3 py-1.5 text-[var(--color-text)]">{ap.actor}</td>
                    <td className="px-3 py-1.5 text-[var(--color-text-faint)]">{ap.note || '—'}</td>
                    <td className="px-3 py-1.5 text-[var(--color-text-faint)]">{ap.approved_at.slice(0, 19).replace('T', ' ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Add approval form */}
        <div className="space-y-2 pt-1 border-t border-[var(--color-border)]">
          <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Record approval</p>
          <div className="flex items-center gap-2">
            <label className="flex-1 block">
              <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Role</span>
              <select
                value={role}
                onChange={e => setRole(e.target.value)}
                className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
              >
                <option value="reviewer">reviewer</option>
                <option value="security">security</option>
                <option value="compliance">compliance</option>
              </select>
            </label>
          </div>
          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Note (optional)</span>
            <textarea
              value={note}
              onChange={e => setNote(e.target.value)}
              placeholder="e.g. reviewed model card, no issues found"
              rows={2}
              className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-xs focus:border-[var(--color-accent)] focus:outline-none"
            />
          </label>
          <div className="flex gap-2 justify-end">
            <button
              onClick={onClose}
              className="px-3 py-1.5 rounded border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            >Close</button>
            <button
              disabled={busy}
              onClick={onSubmit}
              className="px-3 py-1.5 rounded border border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)] text-xs font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
            >{busy ? 'Recording…' : 'Record approval'}</button>
          </div>
        </div>
      </div>
    </div>
  );
}
