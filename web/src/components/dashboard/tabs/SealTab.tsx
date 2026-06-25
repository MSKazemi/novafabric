import { useState, useEffect, useCallback } from 'react';
import { clsx } from 'clsx';
import { api, type SealPolicyResponse, type SealProposalSummary, type SealVerifyResponse, type SealBypassResponse, type CapsuleVerifyResult } from '../../../lib/api';
import { SuggestInput } from '../../ui/SuggestInput';
import EmptyState from '../../ui/EmptyState';
import CopyButton from '../../ui/CopyButton';
import ActionButton from '../../ui/ActionButton';
import { useMutation } from '../../../lib/useMutation';

// ── helpers ──────────────────────────────────────────────────────────────────

function fmt(ts: string | null | undefined): string {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function truncate(s: string, n = 20): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

// ── sub-components ────────────────────────────────────────────────────────────

function PolicyPanel({ policy, error }: { policy: SealPolicyResponse | null; error: string | null }) {
  const labelClass = 'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';
  const valueClass = 'text-xs font-mono text-[var(--color-text)]';

  // Unconfigured (200 with configured:false) or a genuine fetch error both
  // render the same "no policy yet" empty state.
  if (error || (policy && (policy.configured === false || !policy.predicate))) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)] mb-3">
          Promotion Policy
        </p>
        <EmptyState
          variant="inline"
          message="No promotion policy configured"
          cliCommand="nova policy sign --proposer-key proposer.pem --proposer-cert proposer.crt ..."
        />
      </div>
    );
  }

  if (!policy) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 text-xs text-[var(--color-text-faint)]">
        Loading policy…
      </div>
    );
  }

  // Non-null: the guard above returns early when predicate is missing.
  const pred = policy.predicate!;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          Promotion Policy
        </p>
        <span className="font-mono text-[10px] px-2 py-0.5 rounded bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)] text-[var(--color-accent)]">
          v{policy.version}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-4 text-xs">
        <div className="space-y-1">
          <p className={labelClass}>Proposer key IDs</p>
          {pred.proposer_key_ids && pred.proposer_key_ids.length > 0 ? (
            <ul className="space-y-0.5">
              {pred.proposer_key_ids.map((k) => (
                <li key={k} className={`${valueClass} truncate`} title={k}>{k}</li>
              ))}
            </ul>
          ) : (
            <p className={valueClass}>—</p>
          )}
        </div>
        <div className="space-y-1">
          <p className={labelClass}>Approver key IDs</p>
          {pred.approver_key_ids && pred.approver_key_ids.length > 0 ? (
            <ul className="space-y-0.5">
              {pred.approver_key_ids.map((k) => (
                <li key={k} className={`${valueClass} truncate`} title={k}>{k}</li>
              ))}
            </ul>
          ) : (
            <p className={valueClass}>—</p>
          )}
        </div>
      </div>

      <div className="flex items-center gap-6 text-[10px] font-mono text-[var(--color-text-faint)]">
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">bypass window</span>
          {pred.bypass_valid_duration_hours ?? '—'}h
        </span>
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">self-approval</span>
          {pred.self_approval ? 'allowed' : 'prohibited'}
        </span>
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">threshold</span>
          {pred.approval_threshold ?? 1}
        </span>
        <span>
          <span className="text-[var(--color-text-muted)] mr-1">created</span>
          {fmt(policy.created_at)}
        </span>
      </div>
    </div>
  );
}

interface ProposalCardProps {
  proposal: SealProposalSummary;
  onVerify: (uuid: string) => void;
  verifying: boolean;
  verifyResult: SealVerifyResponse | undefined;
}

function ProposalCard({ proposal, onVerify, verifying, verifyResult }: ProposalCardProps) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
      {/* Header row */}
      <div className="px-4 py-3 flex items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-sunken)]">
        <span className="font-mono text-[10px] text-[var(--color-text-faint)] flex-1 truncate" title={proposal.uuid}>
          {truncate(proposal.uuid, 28)}
        </span>
        <CopyButton text={proposal.uuid} label="UUID" />
        <span
          className={clsx(
            'text-[9px] font-mono uppercase tracking-wider px-2 py-0.5 rounded border shrink-0',
            proposal.has_approval
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)] text-[var(--color-status-success)]'
              : 'border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)] text-[var(--color-status-pending)]',
          )}
        >
          {proposal.has_approval ? 'Approved' : 'Awaiting approval'}
        </span>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-2 text-xs">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">
              Proposer
            </p>
            <p className="font-mono text-[var(--color-text)] truncate" title={proposal.proposer_subject}>
              {proposal.proposer_subject || '—'}
            </p>
          </div>
          <div>
            <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">
              Proposed at
            </p>
            <p className="text-[var(--color-text-muted)]">{fmt(proposal.timestamp)}</p>
          </div>
        </div>

        {proposal.justification && (
          <div>
            <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">
              Justification
            </p>
            <p className="text-[var(--color-text-muted)]">{proposal.justification}</p>
          </div>
        )}

        {proposal.has_approval && (
          <div className="grid grid-cols-2 gap-3 pt-1 border-t border-[var(--color-border)]">
            <div>
              <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">
                Approver
              </p>
              <p className="font-mono text-[var(--color-text)] truncate" title={proposal.approver_subject ?? ''}>
                {proposal.approver_subject || '—'}
              </p>
            </div>
            <div>
              <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">
                Approved at
              </p>
              <p className="text-[var(--color-text-muted)]">{fmt(proposal.approval_timestamp)}</p>
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--color-text-faint)]">
          <span>policy v{proposal.policy_version || '—'}</span>
        </div>
      </div>

      {/* Verify button + result */}
      <div className="px-4 py-3 border-t border-[var(--color-border)] space-y-2">
        <button
          onClick={() => onVerify(proposal.uuid)}
          disabled={!proposal.has_approval || verifying}
          title={!proposal.has_approval ? 'Approval required before verification' : 'Run five-check SoD verifier'}
          className={clsx(
            'text-xs font-mono px-3 py-1.5 rounded border transition-colors',
            !proposal.has_approval || verifying
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {verifying ? 'verifying…' : 'Verify SoD chain'}
        </button>

        {verifyResult && <VerifyResultPanel result={verifyResult} />}
      </div>
    </div>
  );
}

function VerifyResultPanel({ result }: { result: SealVerifyResponse }) {
  return (
    <div
      className={clsx(
        'rounded border p-3 space-y-2',
        result.passed
          ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
          : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
      )}
    >
      <div className="flex items-center gap-2">
        <span
          className={clsx(
            'text-xs font-mono font-bold',
            result.passed ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
          )}
        >
          {result.passed ? '✓ SoD verification passed' : `✗ SoD verification failed (exit ${result.exit_code})`}
        </span>
      </div>

      {!result.passed && (
        <p className="text-xs text-[var(--color-text-muted)]">{result.message}</p>
      )}

      {result.check_results.length > 0 && (
        <ul className="space-y-0.5">
          {result.check_results.map((c) => (
            <li key={c.check} className="flex items-start gap-2 text-[11px] font-mono">
              <span
                className={clsx(
                  'shrink-0 mt-px',
                  c.passed ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
                )}
              >
                {c.passed ? '✓' : '✗'}
              </span>
              <span className="text-[var(--color-text-muted)]">
                Check {c.check}: {c.name}
                {!c.passed && c.message !== 'ok' && (
                  <span className="block text-[var(--color-status-failure)] mt-0.5">{c.message}</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Bypass SoD panel ─────────────────────────────────────────────────────────

function BypassSodPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [durationHours, setDurationHours] = useState(24);
  const [reason, setReason] = useState('');
  const [keyPem, setKeyPem] = useState('');
  const [certPem, setCertPem] = useState('');
  const [targetEnv, setTargetEnv] = useState('production');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<SealBypassResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const reasonLen = reason.length;
  const canSubmit = capsuleId.trim() && reasonLen >= 50 && keyPem.trim() && certPem.trim() && !loading;

  const submit = useCallback(async () => {
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.sealBypass(capsuleId.trim(), {
        reason,
        duration_hours: durationHours,
        key_pem: keyPem.trim(),
        cert_pem: certPem.trim(),
        target_env: targetEnv,
      });
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [canSubmit, capsuleId, reason, durationHours, keyPem, certPem, targetEnv]);

  const cliCmd = `nova seal bypass --capsule-id ${capsuleId.trim() || '<run_id>'} --duration ${durationHours}h --key admin.pem --justification "${reason.slice(0, 60) || 'Emergency hotfix – authorized by CTO'}"`;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          Bypass SoD Requirement
        </p>
      </div>
      <p className="text-xs text-[var(--color-text-muted)]">
        Create a time-limited bypass of the maker-checker SoD requirement.
        Max 7 days. The bypass is DSSE-signed and permanently logged in the audit trail.
        Requires justification and signing key. (ADR-0059)
      </p>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule ID
          </label>
          <SuggestInput
            value={capsuleId}
            onChange={setCapsuleId}
            suggestions={runIds}
            placeholder="run_2024_..."
            className={inputClass}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Duration (hours, max 168)
          </label>
          <input
            type="number"
            min={1}
            max={168}
            value={durationHours}
            onChange={(e) => setDurationHours(Math.max(1, Math.min(168, Number(e.target.value))))}
            className={inputClass}
          />
        </div>
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] flex items-center gap-2">
          Justification / Reason
          <span className={clsx('text-[9px]', reasonLen >= 50 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {reasonLen}/50 min
          </span>
        </label>
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          rows={3}
          placeholder="Emergency hotfix – authorized by CTO (must be at least 50 characters)"
          className={clsx(inputClass, 'resize-y')}
        />
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Target Environment
        </label>
        <select
          value={targetEnv}
          onChange={(e) => setTargetEnv(e.target.value)}
          className={inputClass}
        >
          <option value="production">production</option>
          <option value="staging">staging</option>
          <option value="development">development</option>
        </select>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Private Key PEM (ECDSA P-256)
          </label>
          <textarea
            value={keyPem}
            onChange={(e) => setKeyPem(e.target.value)}
            rows={5}
            placeholder="-----BEGIN EC PRIVATE KEY-----&#10;...&#10;-----END EC PRIVATE KEY-----"
            className={clsx(inputClass, 'resize-y')}
            spellCheck={false}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Certificate PEM (X.509)
          </label>
          <textarea
            value={certPem}
            onChange={(e) => setCertPem(e.target.value)}
            rows={5}
            placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----"
            className={clsx(inputClass, 'resize-y')}
            spellCheck={false}
          />
        </div>
      </div>

      <button
        onClick={submit}
        disabled={!canSubmit}
        className={clsx(
          'text-xs font-mono px-4 py-2 rounded border transition-colors',
          canSubmit
            ? 'border-[color-mix(in_oklab,var(--color-status-failure)_60%,transparent)] text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)]'
            : 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed',
        )}
      >
        {loading ? 'creating bypass…' : 'Create SoD bypass'}
      </button>

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] p-3 space-y-2">
          <p className="text-xs font-mono font-bold text-[var(--color-status-success)]">
            ✓ Bypass created — permanently logged in audit trail
          </p>
          <div className="grid grid-cols-2 gap-3 text-[11px] font-mono">
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">UUID</p>
              <p className="text-[var(--color-text)] break-all">{result.bypass_uuid}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">Authorized by</p>
              <p className="text-[var(--color-text)] truncate">{result.authorized_by}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">Valid until</p>
              <p className="text-[var(--color-text-muted)]">{fmt(result.valid_until)}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">Target env</p>
              <p className="text-[var(--color-text-muted)]">{result.target_env}</p>
            </div>
          </div>
        </div>
      )}

      {/* CLI reference */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all">
            {cliCmd}
          </pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Merkle Log Verify panel ───────────────────────────────────────────────────

interface MerkleLogResult {
  consistent: boolean;
  entry_count: number;
  message: string;
  capsule_included?: boolean;
}

function CheckRow({ label, ok }: { label: string; ok: boolean | undefined }) {
  if (ok === undefined) return null;
  return (
    <div className="flex items-center gap-2 text-[11px] font-mono">
      <span className={ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>
        {ok ? '✓' : '✗'}
      </span>
      <span className={ok ? 'text-[var(--color-text-muted)]' : 'text-[var(--color-text)]'}>{label}</span>
      <span className={clsx(
        'ml-auto font-medium',
        ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
      )}>
        {ok ? 'OK' : 'FAIL'}
      </span>
    </div>
  );
}

// ── CapsuleVerifyPanel — mirrors `nova verify <capsule>` ─────────────────────

function CapsuleVerifyPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<CapsuleVerifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const verify = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.capsuleVerify(id);
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [runId]);

  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full';

  const cliCmd = runId.trim() ? `nova verify ${runId.trim()}` : 'nova verify <capsule_dir>';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        Capsule Integrity Verify
      </p>
      <p className="text-xs text-[var(--color-text-muted)]">
        Verify a capsule's cryptographic seal — DSSE signature (ECDSA P-256), RFC 3161 timestamp, and
        Merkle log inclusion. Requires NovaSeal to be configured. (ADR-0041)
      </p>

      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Run ID
          </label>
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="01KRK8..."
            className={inputClass}
            onEnter={verify}
          />
        </div>
        <button
          onClick={verify}
          disabled={loading || !runId.trim()}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
            loading || !runId.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'verifying…' : 'Verify capsule'}
        </button>
      </div>

      {/* CLI reference */}
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
        <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap">{cliCmd}</pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cliCmd} label="CLI" />
        </div>
      </div>

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && (
        <div className={clsx(
          'rounded border p-3 space-y-2',
          result.sealed === false
            ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
            : result.configured === false
              ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
              : result.valid
                ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
                : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
        )}>
          {result.sealed === false ? (
            <p className="text-xs text-[var(--color-status-pending)]">Not sealed — {result.message}</p>
          ) : result.configured === false ? (
            <p className="text-xs text-[var(--color-status-pending)]">{result.message}</p>
          ) : (
            <>
              <div className="space-y-1">
                <CheckRow label="Signature (DSSE ECDSA P-256)" ok={result.signature_ok} />
                <CheckRow label="Timestamp (RFC 3161)" ok={result.timestamp_ok} />
                <CheckRow label="Merkle log inclusion" ok={result.log_integrity_ok} />
              </div>
              {result.capsule_id && (
                <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
                  capsule_id: {result.capsule_id}
                </p>
              )}
              {result.errors && result.errors.length > 0 && (
                <ul className="text-[10px] text-[var(--color-status-failure)] space-y-0.5 pt-1 border-t border-[color-mix(in_oklab,var(--color-status-failure)_20%,transparent)]">
                  {result.errors.map((e, i) => <li key={i}>✗ {e}</li>)}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── SigstoreSignPanel — mirrors `nova seal sign --backend sigstore` ──────────

function SigstoreSignPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean; capsule_id: string; bundle_path: string;
    log_index: number | null; identity: string | null; note: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notInstalled, setNotInstalled] = useState(false);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const sign = useCallback(async () => {
    const id = capsuleId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setNotInstalled(false);
    try {
      const res = await api.sigstoreSign(id);
      setResult(res);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('501') || msg.toLowerCase().includes('not installed')) {
        setNotInstalled(true);
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [capsuleId]);

  const cliCmd = capsuleId.trim()
    ? `nova seal sign --backend sigstore --capsule-id ${capsuleId.trim()}`
    : 'nova seal sign --backend sigstore --capsule-id <capsule_id>';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        Sigstore Keyless Sign (nova seal sign --backend sigstore)
      </p>
      <p className="text-xs text-[var(--color-text-muted)]">
        Sign a capsule artifact using Sigstore keyless signing — obtains a short-lived
        Fulcio certificate via OIDC and records an inclusion proof in Rekor. (ADR-0071)
      </p>

      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule ID
          </label>
          <SuggestInput
            value={capsuleId}
            onChange={setCapsuleId}
            suggestions={runIds}
            placeholder="01KRK8..."
            className={inputClass}
            onEnter={sign}
          />
        </div>
        <button
          onClick={sign}
          disabled={loading || !capsuleId.trim()}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
            loading || !capsuleId.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'signing… (~10s)' : 'Sign with Sigstore'}
        </button>
      </div>

      {/* CLI reference */}
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
        <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap">
          {cliCmd}
        </pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cliCmd} label="CLI" />
        </div>
      </div>

      {notInstalled && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] px-3 py-2">
          <p className="text-xs text-[var(--color-status-pending)] font-mono">
            novafabric[sigstore] not installed — run:{' '}
            <span className="font-bold">pip install novafabric[sigstore]</span>
          </p>
        </div>
      )}

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] p-3 space-y-1.5">
          <p className="text-xs font-mono font-bold text-[var(--color-status-success)]">
            ✓ Sigstore bundle stored
          </p>
          <p className="text-[11px] font-mono text-[var(--color-text-faint)] break-all">
            path: {result.bundle_path}
          </p>
          {result.log_index !== null && (
            <p className="text-[11px] font-mono text-[var(--color-text-muted)]">
              rekor log index: {result.log_index}
            </p>
          )}
          {result.identity !== null && (
            <p className="text-[11px] font-mono text-[var(--color-text-muted)]">
              identity: {result.identity}
            </p>
          )}
          <p className="text-[10px] text-[var(--color-text-faint)]">{result.note}</p>
        </div>
      )}
    </div>
  );
}

// ── SigstoreVerifyPanel — mirrors `nova verify --backend sigstore` ────────────

function SigstoreVerifyPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean; capsule_id: string; valid: boolean;
    identity: string | null; rekor_log_index: number | null; error: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notInstalled, setNotInstalled] = useState(false);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const verify = useCallback(async () => {
    const id = capsuleId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setNotInstalled(false);
    try {
      const res = await api.sigstoreVerify(id);
      setResult(res);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('501') || msg.toLowerCase().includes('not installed')) {
        setNotInstalled(true);
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [capsuleId]);

  const cliCmd = capsuleId.trim()
    ? `nova verify --backend sigstore --capsule-id ${capsuleId.trim()}`
    : 'nova verify --backend sigstore --capsule-id <capsule_id>';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        Sigstore Bundle Verify (nova verify --backend sigstore)
      </p>
      <p className="text-xs text-[var(--color-text-muted)]">
        Verify a stored Sigstore bundle — checks the Fulcio certificate, signature,
        and Rekor inclusion proof offline. (ADR-0071)
      </p>

      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule ID
          </label>
          <SuggestInput
            value={capsuleId}
            onChange={setCapsuleId}
            suggestions={runIds}
            placeholder="01KRK8..."
            className={inputClass}
            onEnter={verify}
          />
        </div>
        <button
          onClick={verify}
          disabled={loading || !capsuleId.trim()}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
            loading || !capsuleId.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'verifying…' : 'Verify Sigstore Bundle'}
        </button>
      </div>

      {/* CLI reference */}
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
        <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap">
          {cliCmd}
        </pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cliCmd} label="CLI" />
        </div>
      </div>

      {notInstalled && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] px-3 py-2">
          <p className="text-xs text-[var(--color-status-pending)] font-mono">
            novafabric[sigstore] not installed — run:{' '}
            <span className="font-bold">pip install novafabric[sigstore]</span>
          </p>
        </div>
      )}

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && !result.ok && result.error && result.error.includes('No Sigstore bundle') ? (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] px-3 py-2">
          <p className="text-xs text-[var(--color-status-pending)]">
            No Sigstore bundle found for this capsule
          </p>
        </div>
      ) : result && (
        <div
          className={clsx(
            'rounded border p-3 space-y-1.5',
            result.valid
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
          )}
        >
          <div className="flex items-center gap-2">
            <span
              className={clsx(
                'text-xs font-mono font-bold px-2 py-0.5 rounded',
                result.valid
                  ? 'bg-[color-mix(in_oklab,var(--color-status-success)_20%,transparent)] text-[var(--color-status-success)]'
                  : 'bg-[color-mix(in_oklab,var(--color-status-failure)_20%,transparent)] text-[var(--color-status-failure)]',
              )}
            >
              {result.valid ? 'VALID' : 'INVALID'}
            </span>
          </div>
          {result.identity !== null && (
            <p className="text-[11px] font-mono text-[var(--color-text-muted)]">
              identity: {result.identity}
            </p>
          )}
          {result.rekor_log_index !== null && (
            <p className="text-[11px] font-mono text-[var(--color-text-muted)]">
              rekor log index: {result.rekor_log_index}
            </p>
          )}
          {result.error && (
            <p className="text-[11px] text-[var(--color-status-failure)]">
              {result.error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function MerkleLogVerifyPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<MerkleLogResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const verify = useCallback(async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.sealLogVerify(capsuleId.trim() || undefined);
      setResult(res as MerkleLogResult);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [capsuleId]);

  const cliCmd = capsuleId.trim()
    ? `nova seal log verify --capsule-id ${capsuleId.trim()}`
    : 'nova seal log verify';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
        Merkle Log Verify
      </p>
      <p className="text-xs text-[var(--color-text-muted)]">
        Verify Merkle log consistency — checks all entries are cryptographically
        linked and none have been tampered with. (ADR-0041)
      </p>

      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule ID (optional)
          </label>
          <SuggestInput
            value={capsuleId}
            onChange={setCapsuleId}
            suggestions={runIds}
            placeholder="leave blank to verify full log"
            className={inputClass}
            onEnter={verify}
          />
        </div>
        <button
          onClick={verify}
          disabled={loading}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
            loading
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'verifying…' : 'Verify log integrity'}
        </button>
      </div>

      {/* CLI reference */}
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
        <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap">
          {cliCmd}
        </pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cliCmd} label="CLI" />
        </div>
      </div>

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && (
        <div
          className={clsx(
            'rounded border p-3 space-y-2',
            result.consistent
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
          )}
        >
          <div className="flex items-center gap-3">
            <span
              className={clsx(
                'text-xs font-mono font-bold',
                result.consistent ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
              )}
            >
              {result.consistent ? '✓ Log consistent' : '✗ Log inconsistent'}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.entry_count} entr{result.entry_count === 1 ? 'y' : 'ies'}
            </span>
            {result.capsule_included !== undefined && (
              <span
                className={clsx(
                  'text-[10px] font-mono',
                  result.capsule_included
                    ? 'text-[var(--color-status-success)]'
                    : result.entry_count === 0
                      ? 'text-[var(--color-text-faint)]'
                      : 'text-[var(--color-status-failure)]',
                )}
              >
                {result.capsule_included
                  ? 'capsule: included'
                  : result.entry_count === 0
                    ? 'seal log is empty'
                    : 'capsule: not found'}
              </span>
            )}
          </div>
          {result.message && (
            <p className="text-xs text-[var(--color-text-muted)]">{result.message}</p>
          )}
        </div>
      )}
    </div>
  );
}

// ── main tab ──────────────────────────────────────────────────────────────────

/** Forward-secure per-node signing key ratchet (ADR-0089). */
function RatchetPanel() {
  const [nodeId, setNodeId] = useState('');
  const [state, setState] = useState<Record<string, unknown> | null>(null);
  const [epochs, setEpochs] = useState<number[]>([]);

  const status = useMutation((id: string) => api.ratchetStatus(id), {
    silentSuccess: true,
    onSuccess: (r) => { setState(r.state ?? null); setEpochs(r.registry_epochs ?? []); },
  });
  const init = useMutation((id: string) => api.ratchetInit(id), {
    successMessage: 'Ratchet initialised', onSuccess: (r) => setState(r.state ?? null),
  });
  const rotate = useMutation((id: string) => api.ratchetRotate(id), {
    successMessage: 'Rotated to next epoch', onSuccess: (r) => setState(r.state ?? null),
  });

  const id = nodeId.trim();
  return (
    <div className="rounded border border-[var(--color-border)] p-4 space-y-2">
      <h3 className="text-sm font-medium text-[var(--color-text)]">Signing key ratchet <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">ADR-0089</span></h3>
      <p className="text-xs text-[var(--color-text-muted)]">Forward-secure per-node epochs. Rotation erases the previous chain key (best-effort).</p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
          Node ID
          <input value={nodeId} onChange={(e) => setNodeId(e.target.value)} placeholder="node-a" className="w-48 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]" />
        </label>
        <ActionButton onClick={() => status.run(id)} pending={status.pending} disabled={!id}>Status</ActionButton>
        <ActionButton onClick={() => init.run(id)} pending={init.pending} disabled={!id} variant="primary">Init epoch 0</ActionButton>
        <ActionButton
          onClick={() => rotate.run(id)}
          pending={rotate.pending}
          disabled={!id}
          variant="danger"
          confirm={{ title: 'Rotate signing epoch?', body: 'Advances to the next epoch and erases the previous chain key (irreversible).', confirmLabel: 'Rotate', tone: 'danger' }}
        >
          Rotate
        </ActionButton>
      </div>
      {state && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-xs font-mono space-y-1">
          <div>node: {String(state.node_id ?? '—')}</div>
          <div>epoch: {String(state.epoch ?? '—')}</div>
          <div>rotated_at: {String(state.rotated_at ?? '—')}</div>
          {epochs.length > 0 && <div className="text-[var(--color-text-faint)]">registry epochs: [{epochs.join(', ')}]</div>}
        </div>
      )}
    </div>
  );
}

export default function SealTab() {
  const [policy, setPolicy] = useState<SealPolicyResponse | null>(null);
  const [policyError, setPolicyError] = useState<string | null>(null);

  const [capsuleId, setCapsuleId] = useState('');
  const [runIds, setRunIds] = useState<string[]>([]);
  const [proposals, setProposals] = useState<SealProposalSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [proposalsError, setProposalsError] = useState<string | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  // verifyResult keyed by proposal UUID
  const [verifyResult, setVerifyResult] = useState<Record<string, SealVerifyResponse>>({});
  const [verifying, setVerifying] = useState<Record<string, boolean>>({});

  // Load policy on mount
  useEffect(() => {
    api.sealGetPolicy()
      .then(setPolicy)
      .catch(() => setPolicyError('not found'));
  }, []);

  // Load run IDs for SuggestInput
  useEffect(() => {
    api.listRuns()
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {});
  }, []);

  const loadProposals = useCallback(async () => {
    if (!capsuleId.trim()) return;
    setLoading(true);
    setProposalsError(null);
    setProposals([]);
    setVerifyResult({});
    try {
      const list = await api.sealListProposals(capsuleId.trim());
      setProposals(list);
      setLoadedFor(capsuleId.trim());
    } catch (e) {
      setProposalsError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [capsuleId]);

  const handleVerify = useCallback(async (proposalUuid: string) => {
    if (!loadedFor) return;
    setVerifying((v) => ({ ...v, [proposalUuid]: true }));
    try {
      const result = await api.sealVerify(loadedFor);
      setVerifyResult((prev) => ({ ...prev, [proposalUuid]: result }));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setVerifyResult((prev) => ({
        ...prev,
        [proposalUuid]: {
          capsule_id: loadedFor,
          passed: false,
          exit_code: -1,
          message: msg,
          check_results: [],
        },
      }));
    } finally {
      setVerifying((v) => ({ ...v, [proposalUuid]: false }));
    }
  }, [loadedFor]);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between text-xs">
        <div>
          <p className="text-sm font-semibold text-[var(--color-text)]">NovaSeal Maker-Checker</p>
          <p className="text-[var(--color-text-faint)] text-[10px] font-mono mt-0.5">ADR-0059 · nova seal propose / approve / verify</p>
        </div>
      </div>

      {/* Policy panel */}
      <PolicyPanel policy={policy} error={policyError} />

      {/* Capsule lookup */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          Capsule proposals
        </p>

        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1">
            <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
              Capsule ID
            </label>
            <SuggestInput
              value={capsuleId}
              onChange={setCapsuleId}
              suggestions={runIds}
              placeholder="run_2024_..."
              className={inputClass}
              onEnter={loadProposals}
            />
          </div>
          <button
            onClick={loadProposals}
            disabled={!capsuleId.trim() || loading}
            className={clsx(
              'text-xs font-mono px-4 py-1.5 rounded border transition-colors shrink-0',
              !capsuleId.trim() || loading
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {loading ? 'loading…' : 'Load proposals'}
          </button>
        </div>

        {/* Error */}
        {proposalsError && (
          <p className="text-xs text-[var(--color-status-failure)]">
            Error: {proposalsError}
          </p>
        )}

        {/* Proposals list */}
        {!loading && loadedFor && proposals.length === 0 && (
          <EmptyState
            variant="inline"
            message={`No proposals found for capsule "${loadedFor}"`}
            cliCommand={`nova seal propose --capsule-id ${loadedFor} --key proposer.pem --cert proposer.crt --justification "..."`}
          />
        )}

        {proposals.length > 0 && (
          <div className="space-y-3">
            {loadedFor && (
              <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
                {proposals.length} proposal{proposals.length !== 1 ? 's' : ''} for{' '}
                <span className="text-[var(--color-text)]">{loadedFor}</span>
              </p>
            )}
            {proposals.map((proposal) => (
              <ProposalCard
                key={proposal.uuid}
                proposal={proposal}
                onVerify={handleVerify}
                verifying={!!verifying[proposal.uuid]}
                verifyResult={verifyResult[proposal.uuid]}
              />
            ))}
          </div>
        )}
      </div>

      {/* Bypass SoD Requirement */}
      <BypassSodPanel runIds={runIds} />

      {/* Capsule integrity verify */}
      <CapsuleVerifyPanel runIds={runIds} />

      {/* Sigstore keyless signing / verification (v0.44.0) */}
      <SigstoreSignPanel runIds={runIds} />
      <SigstoreVerifyPanel runIds={runIds} />

      {/* Merkle Log Verify */}
      <MerkleLogVerifyPanel runIds={runIds} />

      {/* Forward-secure signing key ratchet (ADR-0089) */}
      <RatchetPanel />
    </div>
  );
}
