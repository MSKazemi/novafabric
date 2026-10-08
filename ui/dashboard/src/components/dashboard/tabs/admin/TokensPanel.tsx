// Token issue dialog + token table row. Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { useState } from 'react';
import { clsx } from 'clsx';
import { api, type TokenRecord } from '../../../../lib/api';
import { ConfirmDialog } from './helpers';

export function IssueTokenDialog({
  onClose,
  onIssued,
}: {
  onClose: () => void;
  onIssued: () => void;
}) {
  const [label, setLabel] = useState('dashboard-issued');
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<{ token: string; fingerprint: string; warning: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!label.trim()) return;
    setConfirming(true);
  };

  const handleConfirm = async () => {
    setConfirming(false);
    setSubmitting(true);
    setError(null);
    try {
      const r = await api.issueToken(label.trim());
      setResult({ token: r.token, fingerprint: r.fingerprint, warning: r.warning });
      onIssued();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleCopy = async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.token);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-md rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5 shadow-xl">
        <h3 className="text-sm font-semibold text-[var(--color-text)] mb-4">Issue new token</h3>

        {result ? (
          <div className="space-y-3">
            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] p-3 text-xs text-[var(--color-status-pending)]">
              {result.warning}
            </div>
            <div>
              <p className="text-[10px] font-mono text-[var(--color-text-faint)] mb-1">
                Fingerprint: <span className="text-[var(--color-text-muted)]">{result.fingerprint}</span>
              </p>
              <div className="flex items-center gap-2">
                <code className="flex-1 min-w-0 truncate font-mono text-xs bg-[var(--color-bg-sunken)] border border-[var(--color-border)] rounded px-2 py-1.5 text-[var(--color-text)]">
                  {result.token}
                </code>
                <button
                  onClick={handleCopy}
                  className="shrink-0 text-[10px] font-mono px-2 py-1.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
                >
                  {copied ? 'copied!' : 'copy'}
                </button>
              </div>
            </div>
            <div className="flex justify-end">
              <button
                onClick={onClose}
                className="px-3 py-1.5 rounded text-xs font-medium bg-[var(--color-accent)] text-white hover:opacity-90 transition-colors"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-[10px] font-mono text-[var(--color-text-faint)] mb-1">
                Label
              </label>
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="dashboard-issued"
                className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]"
              />
            </div>
            {error && (
              <p className="text-xs text-[var(--color-status-failure)] font-mono">{error}</p>
            )}
            <div className="flex gap-2 justify-end">
              <button
                type="button"
                onClick={onClose}
                className="px-3 py-1.5 rounded text-xs font-medium border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting || !label.trim()}
                className="px-3 py-1.5 rounded text-xs font-medium bg-[var(--color-accent)] text-white hover:opacity-90 disabled:opacity-50 transition-colors"
              >
                {submitting ? 'Issuing…' : 'Issue token'}
              </button>
            </div>
          </form>
        )}
      </div>

      {confirming && (
        <ConfirmDialog
          title="Issue new token?"
          message={`This will create a new local session token with label "${label}". The token value will only be shown once.`}
          confirmLabel="Issue"
          onConfirm={handleConfirm}
          onCancel={() => setConfirming(false)}
        />
      )}
    </div>
  );
}

export function TokenRow({
  token,
  onRevoked,
}: {
  token: TokenRecord;
  onRevoked: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleRevoke = async () => {
    setConfirming(false);
    setRevoking(true);
    setError(null);
    try {
      await api.revokeToken(token.fingerprint);
      onRevoked();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRevoking(false);
    }
  };

  const created = token.created_at ? token.created_at.slice(0, 19).replace('T', ' ') : '—';

  return (
    <tr className="border-t border-[var(--color-border)] text-xs">
      <td className="px-3 py-2 font-medium text-[var(--color-text)]">{token.label || '—'}</td>
      <td className="px-3 py-2 font-mono text-[var(--color-text-muted)]">{token.fingerprint}</td>
      <td className="px-3 py-2 font-mono text-[var(--color-text-faint)]">{created}</td>
      <td className="px-3 py-2">
        <span
          className={clsx(
            'text-2xs font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border',
            token.revoked
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]'
              : 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]',
          )}
        >
          {token.revoked ? 'revoked' : 'active'}
        </span>
      </td>
      <td className="px-3 py-2">
        {!token.revoked && (
          <>
            <button
              onClick={() => setConfirming(true)}
              disabled={revoking}
              className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-status-failure)] text-[var(--color-status-failure)] hover:bg-[var(--color-status-failure)] hover:text-white disabled:opacity-50 transition-colors"
            >
              {revoking ? 'revoking…' : 'revoke'}
            </button>
            {error && (
              <span className="ml-2 text-[10px] text-[var(--color-status-failure)] font-mono">{error}</span>
            )}
          </>
        )}
      </td>
      {confirming && (
        <td className="hidden">
          {/* Dialog is rendered outside the table DOM via portal-like pattern */}
        </td>
      )}
      {confirming && (
        // Rendered via inline approach — positioned absolute over the table
        <ConfirmDialog
          title="Revoke token?"
          message={`This will revoke token "${token.label}" (${token.fingerprint}). It can no longer be used to authenticate. This cannot be undone.`}
          confirmLabel="Revoke"
          danger
          onConfirm={handleRevoke}
          onCancel={() => setConfirming(false)}
        />
      )}
    </tr>
  );
}
