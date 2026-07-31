// Capsule proposals lookup + SoD verify. ProposalCard / VerifyResultPanel and
// the proposals-lookup state machine extracted verbatim from SealTab.tsx
// (dashboard-modernization split) — the lookup state simply moved from the
// tab shell into this panel, render output unchanged.
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api, type SealProposalSummary, type SealVerifyResponse } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import EmptyState from '../../../ui/EmptyState';
import CopyButton from '../../../ui/CopyButton';
import { fmt, truncate } from './helpers';

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
            'text-2xs font-mono uppercase tracking-wider px-2 py-0.5 rounded border shrink-0',
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

export default function ProposalsPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [proposals, setProposals] = useState<SealProposalSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [proposalsError, setProposalsError] = useState<string | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  // verifyResult keyed by proposal UUID
  const [verifyResult, setVerifyResult] = useState<Record<string, SealVerifyResponse>>({});
  const [verifying, setVerifying] = useState<Record<string, boolean>>({});

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
  );
}
