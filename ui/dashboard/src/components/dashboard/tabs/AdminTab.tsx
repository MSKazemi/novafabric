/**
 * Admin tab — thin shell over the per-panel modules in `./admin/`.
 * The panels were extracted verbatim (behavior frozen); this file only owns
 * the tokens/roles load loop, the Session + Issued-tokens chrome, and the
 * render order. (DD-8 / v0.19.0)
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type RolesListResult, type TokensListResult } from '../../../lib/api';
import { ErrorBox, Loading } from '../helpers';
import {
  ApiKeysPanel,
  DatabaseOpsPanel,
  DoctorPanel,
  FlushJwksCachePanel,
  IngestCapsulePanel,
  IssueTokenDialog,
  NewRunIdPanel,
  Panel,
  RoleManagementPanel,
  SectionHeading,
  TokenRow,
} from './admin';

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
