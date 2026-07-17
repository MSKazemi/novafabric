// Admin panel — token, role management, run utilities, DB ops (DD-8 / v0.19.0)
import { useState, useEffect, useCallback, useRef } from 'react';
import { clsx } from 'clsx';
import {
  api,
  type TokenRecord,
  type TokensListResult,
  type RolesListResult,
  type ApiKeyRow,
} from '../../../lib/api';
import { ErrorBox, Loading } from '../helpers';
import CopyButton from '../../ui/CopyButton';

// ---------- helpers ----------

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-[11px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] mb-3">
      {children}
    </h2>
  );
}

function Panel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={clsx(
        'rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4',
        className,
      )}
    >
      {children}
    </div>
  );
}

// ---------- confirm dialog ----------

function ConfirmDialog({
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
  danger,
}: {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  danger?: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-sm rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5 shadow-xl">
        <h3 className="text-sm font-semibold text-[var(--color-text)] mb-2">{title}</h3>
        <p className="text-xs text-[var(--color-text-muted)] mb-5">{message}</p>
        <div className="flex gap-2 justify-end">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded text-xs font-medium border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className={clsx(
              'px-3 py-1.5 rounded text-xs font-medium transition-colors',
              danger
                ? 'bg-[var(--color-status-failure)] text-white hover:opacity-90'
                : 'bg-[var(--color-accent)] text-white hover:opacity-90',
            )}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------- issue token dialog ----------

function IssueTokenDialog({
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

// ---------- token row ----------

function TokenRow({
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
            'text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border',
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

// ---------- main AdminTab ----------

export default function AdminTab() {
  const [tokensData, setTokensData] = useState<TokensListResult | null>(null);
  const [rolesData, setRolesData] = useState<RolesListResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showIssueDialog, setShowIssueDialog] = useState(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [tokens, roles] = await Promise.all([api.listTokens(), api.listRoles()]);
      setTokensData(tokens);
      setRolesData(roles);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    intervalRef.current = setInterval(load, 30_000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [load]);

  const handleRevoked = useCallback(() => {
    load();
  }, [load]);

  const handleIssued = useCallback(() => {
    load();
  }, [load]);

  if (loading) return <Loading />;
  if (error) return <ErrorBox message={error} onRetry={load} />;

  const sessionFp = tokensData?.session_token_fingerprint ?? '—';
  const tokens = tokensData?.tokens ?? [];

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-sm font-semibold text-[var(--color-text)] mb-0.5">Admin Panel</h1>
        <p className="text-xs text-[var(--color-text-muted)]">
          Token and role management for this NovaFabric server session.
        </p>
      </div>

      {/* Session info */}
      <Panel>
        <SectionHeading>Session</SectionHeading>
        <div className="flex items-center gap-3">
          <span className="text-xs text-[var(--color-text-muted)]">Current session token fingerprint:</span>
          <code className="font-mono text-xs px-2 py-0.5 rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-text)]">
            {sessionFp}
          </code>
        </div>
      </Panel>

      {/* Issued tokens */}
      <Panel>
        <div className="flex items-center justify-between mb-3">
          <SectionHeading>Issued tokens</SectionHeading>
          <button
            onClick={() => setShowIssueDialog(true)}
            className="text-[10px] font-mono px-2.5 py-1 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] transition-colors"
          >
            + issue token
          </button>
        </div>

        {tokens.length === 0 ? (
          <p className="text-xs text-[var(--color-text-faint)] py-2">
            No tokens issued yet. Use the button above to issue a new local session token.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
                  <th className="px-3 py-2 text-left font-medium">Label</th>
                  <th className="px-3 py-2 text-left font-medium">Fingerprint</th>
                  <th className="px-3 py-2 text-left font-medium">Created</th>
                  <th className="px-3 py-2 text-left font-medium">Status</th>
                  <th className="px-3 py-2 text-left font-medium">Action</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => (
                  <TokenRow key={t.fingerprint} token={t} onRevoked={handleRevoked} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-3 text-[10px] font-mono text-[var(--color-text-faint)]">
          Auto-refreshes every 30 seconds. Token values are stored locally in{' '}
          <code>~/.novafabric/tokens.jsonl</code> — they are never transmitted again after issuance.
        </p>
      </Panel>

      {/* API keys (ADR-0193, read-only view) */}
      <ApiKeysPanel />

      {/* Role management */}
      <RoleManagementPanel rolesData={rolesData} onChanged={load} />

      {/* OIDC Configuration */}
      <FlushJwksCachePanel />

      {/* New Run ID */}
      <NewRunIdPanel />

      {/* System diagnostics */}
      <DoctorPanel />

      {/* Reindex capsules */}
      <IngestCapsulePanel />

      {/* Database operations */}
      <DatabaseOpsPanel />

      {showIssueDialog && (
        <IssueTokenDialog
          onClose={() => setShowIssueDialog(false)}
          onIssued={handleIssued}
        />
      )}
    </div>
  );
}

function ApiKeyStatusBadge({ status }: { status: ApiKeyRow['status'] }) {
  const cls =
    status === 'active'
      ? 'text-[var(--color-status-success)]'
      : status === 'expired'
        ? 'text-[var(--color-status-pending)]'
        : 'text-[var(--color-text-faint)]';
  return <span className={`font-mono text-[10px] ${cls}`}>{status}</span>;
}

function ApiKeysPanel() {
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

function NewRunIdPanel() {
  const [runId, setRunId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await api.newRunId();
      setRunId(r.run_id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <Panel>
      <SectionHeading>New Run ID</SectionHeading>
      <div className="space-y-3">
        <p className="text-xs text-[var(--color-text-muted)]">
          Generate a fresh ULID to use as <code className="font-mono text-[10px]">NOVAFABRIC_GLOBAL_RUN_ID</code> before
          running a distributed job (cap-007, FR-27).
        </p>
        <div className="flex items-center gap-2">
          <button
            onClick={generate}
            disabled={loading}
            className={clsx(
              'text-xs font-mono px-3 py-1.5 rounded border transition-colors',
              loading
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {loading ? 'generating…' : 'Generate'}
          </button>
          {runId && (
            <div className="flex items-center gap-1.5 flex-1 min-w-0">
              <code className="text-xs font-mono px-2 py-1 rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-accent)] truncate">
                {runId}
              </code>
              <CopyButton text={runId} label="ID" />
              <CopyButton text={`NOVAFABRIC_GLOBAL_RUN_ID=${runId} nova capture`} label="ENV" />
            </div>
          )}
        </div>
        {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      </div>
    </Panel>
  );
}

function DoctorPanel() {
  const [checks, setChecks] = useState<Array<{ name: string; ok: boolean; detail: string }> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await api.doctor();
      setChecks(r.checks);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <Panel>
      <div className="flex items-center justify-between mb-3">
        <SectionHeading>System Diagnostics</SectionHeading>
        <button
          onClick={run}
          disabled={loading}
          className="text-[10px] font-mono px-2.5 py-1 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
        >
          {loading ? 'running…' : 'nova doctor'}
        </button>
      </div>
      <p className="text-xs text-[var(--color-text-muted)] mb-3">
        Check all NovaFabric subsystems: capsule dir, registry DB, lineage store, OPA binary, NovaSeal, KG store, and Python version.
      </p>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {checks !== null && (
        <div className="rounded border border-[var(--color-border)] overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)]">
              <tr>
                <th className="text-left px-3 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Check</th>
                <th className="text-left px-3 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Status</th>
                <th className="text-left px-3 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {checks.map((c) => (
                <tr key={c.name} className="hover:bg-[var(--color-bg-sunken)] transition-colors">
                  <td className="px-3 py-2 font-mono text-[var(--color-text)] text-[10px]">{c.name}</td>
                  <td className="px-3 py-2">
                    <span className={clsx(
                      'text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded',
                      c.ok
                        ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                        : 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]',
                    )}>
                      {c.ok ? 'ok' : 'fail'}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-[var(--color-text-muted)] text-[10px] font-mono break-all">{c.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ---------- IngestCapsulePanel (v0.46.0) ----------

function IngestCapsulePanel() {
  const inputClass =
    'w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)] disabled:opacity-50';
  const labelClass = 'block text-[10px] font-mono text-[var(--color-text-faint)] mb-1';

  const [runId, setRunId] = useState('');
  const [allMode, setAllMode] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleIngest = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!allMode && !runId.trim()) return;
    setLoading(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await api.ingestCapsule(allMode ? { all: true } : { run_id: runId.trim() });
      if (allMode) {
        setMsg(`indexed ${r.indexed ?? 0} capsule(s)`);
      } else {
        setMsg(`run ${runId.trim()} indexed ${r.is_new ? '(new)' : '(updated)'}`);
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [allMode, runId]);

  return (
    <Panel>
      <SectionHeading>Reindex Capsules</SectionHeading>
      <div className="space-y-3">
        <p className="text-xs text-[var(--color-text-muted)]">
          Index capsule files into the runs metadata store — <code className="font-mono text-[10px]">nova ingest-capsule</code>
        </p>
        <form onSubmit={handleIngest} className="space-y-3">
          <div>
            <label className={labelClass}>Run ID</label>
            <input
              value={runId}
              onChange={(e) => setRunId(e.target.value)}
              placeholder="01JXYZ…"
              disabled={allMode}
              className={inputClass}
            />
          </div>
          <label className="flex items-center gap-2 text-xs text-[var(--color-text-muted)]">
            <input
              type="checkbox"
              checked={allMode}
              onChange={(e) => setAllMode(e.target.checked)}
              className="accent-[var(--color-accent)]"
            />
            Re-index all
          </label>
          <button
            type="submit"
            disabled={loading || (!allMode && !runId.trim())}
            className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white disabled:opacity-50 transition-colors"
          >
            {loading ? 'Ingesting…' : 'Ingest'}
          </button>
        </form>
        {msg && <p className="text-xs text-[var(--color-status-success)] font-mono">{msg}</p>}
        {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
        <CliRefRow cmd="nova ingest-capsule <run_id> | nova ingest-capsule --all" label="CLI reference" />
      </div>
    </Panel>
  );
}

function CliRefRow({ cmd, label }: { cmd: string; label: string }) {
  return (
    <div className="space-y-1">
      <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">{label}</p>
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 pr-16">
        <code className="text-[11px] font-mono text-[var(--color-text-muted)] break-all">{cmd}</code>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cmd} label="copy" />
        </div>
      </div>
    </div>
  );
}

// ---------- RoleManagementPanel (v0.27.0) ----------

const ROLES = ['reader', 'writer', 'admin', 'auditor'] as const;
type RoleValue = (typeof ROLES)[number];

function RoleManagementPanel({
  rolesData,
  onChanged,
}: {
  rolesData: import('../../../lib/api').RolesListResult | null;
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

// ---------- FlushJwksCachePanel (v0.27.0) ----------

function FlushJwksCachePanel() {
  const [result, setResult] = useState<{ ok: boolean; note: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleFlush = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.flushJwksCache();
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <Panel>
      <SectionHeading>OIDC Configuration</SectionHeading>
      <div className="space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-medium text-[var(--color-text)] mb-0.5">Flush JWKS Cache</p>
            <p className="text-xs text-[var(--color-text-muted)]">
              Force the running server to re-fetch its JWKS from the OIDC provider. Use after
              rotating keys or when auth failures suggest a stale cache.
            </p>
          </div>
          <button
            onClick={handleFlush}
            disabled={loading}
            className="shrink-0 text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
          >
            {loading ? 'Flushing…' : 'Flush cache'}
          </button>
        </div>
        {result && (
          <div className={clsx(
            'rounded border px-3 py-2 text-xs font-mono',
            result.ok
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]'
              : 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] text-[var(--color-status-pending)]',
          )}>
            {result.note}
          </div>
        )}
        {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
        <CliRefRow cmd="nova server flush-jwks-cache --server-url <url>" label="CLI reference" />
      </div>
    </Panel>
  );
}

// ---------- DbUpgradeSection (v0.27.0) ----------

function DbUpgradeSection() {
  const inputClass =
    'rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]';

  const [revision, setRevision] = useState('head');
  const [result, setResult] = useState<{ ok: boolean; revision: string; output?: string; note: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleUpgrade = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.dbUpgrade(revision.trim() || 'head');
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [revision]);

  return (
    <div className="space-y-2">
      <p className="text-xs font-medium text-[var(--color-text)]">Alembic schema upgrade</p>
      <p className="text-[10px] text-[var(--color-text-faint)]">
        Apply any pending Alembic migrations (SQLite dev path or Postgres server path).
      </p>
      <div className="flex items-center gap-2">
        <input
          value={revision}
          onChange={(e) => setRevision(e.target.value)}
          placeholder="head"
          className={clsx(inputClass, 'w-32')}
        />
        <button
          onClick={handleUpgrade}
          disabled={loading}
          className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
        >
          {loading ? 'Running…' : 'Run upgrade'}
        </button>
      </div>
      {result && (
        <div className={clsx(
          'rounded border px-3 py-2 text-[10px] font-mono',
          result.ok
            ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]'
            : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]',
        )}>
          <p>{result.note}</p>
          {result.output && <p className="mt-1 text-[var(--color-text-muted)]">{result.output}</p>}
        </div>
      )}
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
    </div>
  );
}

// ---------- CapsuleMigrateSection (v0.27.0) ----------

function CapsuleMigrateSection() {
  const inputClass =
    'w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]';
  const labelClass = 'block text-[10px] font-mono text-[var(--color-text-faint)] mb-1';

  const [source, setSource] = useState('');
  const [output, setOutput] = useState('');
  const [result, setResult] = useState<{
    ok: boolean;
    source: string;
    output: string;
    files_migrated?: number;
    files_updated?: string[];
    lineage_edge_id?: string;
    note: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleMigrate = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!source.trim() || !output.trim()) return;
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.capsuleMigrate(source.trim(), output.trim());
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [source, output]);

  return (
    <div className="space-y-3">
      <p className="text-xs font-medium text-[var(--color-text)]">Capsule format migration</p>
      <p className="text-[10px] text-[var(--color-text-faint)]">
        Convert a v0.1.x capsule directory to v1.0.0 format (ADR-0034 §6).
        The source directory is never modified — output is written to a new path.
      </p>
      <form onSubmit={handleMigrate} className="space-y-3">
        <div>
          <label className={labelClass}>Source path (v0.1.x capsule directory)</label>
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="/path/to/capsule-v01x"
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>Output path (must not already exist)</label>
          <input
            value={output}
            onChange={(e) => setOutput(e.target.value)}
            placeholder="/path/to/capsule-v100"
            className={inputClass}
          />
        </div>
        <button
          type="submit"
          disabled={loading || !source.trim() || !output.trim()}
          className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
        >
          {loading ? 'Migrating…' : 'Migrate capsule'}
        </button>
      </form>
      {result && (
        <div className={clsx(
          'rounded border px-3 py-2 text-[10px] font-mono space-y-1',
          result.ok
            ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]'
            : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]',
        )}>
          <p>{result.note}</p>
          {result.ok && result.files_migrated !== undefined && (
            <p className="text-[var(--color-text-muted)]">Files updated: {result.files_migrated}</p>
          )}
          {result.ok && result.lineage_edge_id && (
            <p className="text-[var(--color-text-muted)]">Lineage edge: {result.lineage_edge_id}</p>
          )}
        </div>
      )}
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      <CliRefRow cmd="nova migrate <source> --output <output>" label="CLI reference" />
    </div>
  );
}

function DatabaseOpsPanel() {
  return (
    <Panel>
      <SectionHeading>Database operations</SectionHeading>
      <div className="space-y-6">
        {/* Interactive: DB upgrade */}
        <DbUpgradeSection />

        {/* CLI-only: migrate-to-postgres */}
        <div className="space-y-2">
          <p className="text-xs font-medium text-[var(--color-text)]">Migrate SQLite → Postgres</p>
          <p className="text-[10px] text-[var(--color-text-faint)]">
            One-time idempotent migration. Requires <code className="font-mono text-[10px]">NOVA_POSTGRES_URL</code>.
          </p>
          <CliRefRow cmd="nova db migrate-to-postgres --postgres-url postgresql://user:pass@host:5432/nova" label="CLI reference" />
        </div>

        {/* CLI-only: rebuild metadata DB */}
        <div className="space-y-2">
          <p className="text-xs font-medium text-[var(--color-text)]">Rebuild metadata DB</p>
          <p className="text-[10px] text-[var(--color-text-faint)]">
            Disaster-recovery path — rebuild the metadata DB from the manifest chain log.
            Completes in minutes, not hours (AC-1, BQ-013).
          </p>
          <CliRefRow cmd="nova rebuild-metadata-db" label="CLI reference" />
        </div>

        {/* Interactive: Capsule format migration */}
        <CapsuleMigrateSection />
      </div>
    </Panel>
  );
}
