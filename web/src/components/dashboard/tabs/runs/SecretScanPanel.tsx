/**
 * Secret scan / redaction-proof viewer for a single run.
 * Extracted verbatim from the former RunsTab monolith — behavior frozen.
 */
import { clsx } from 'clsx';
import type { RedactionProof } from '../../../../lib/api';
import { Loading } from '../../helpers';
import { SEVERITY_STYLE, SeverityBadge } from './severity';

export default function SecretScanPanel({
  runId: _runId,
  capsulePath,
  proof,
  loading,
  error,
  onRunRedact,
}: {
  runId: string;
  capsulePath: string;
  proof: RedactionProof | null;
  loading: boolean;
  error: string | null;
  onRunRedact: () => void;
}) {
  if (loading) return <Loading />;

  if (error) {
    const notFound = error.includes('404') || error.toLowerCase().includes('not found');
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-8 text-center space-y-3">
        <p className="text-sm text-[var(--color-text-muted)]">
          {notFound
            ? 'No redaction proof found for this run.'
            : `Failed to load: ${error}`}
        </p>
        {notFound && (
          <>
            <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
              CLI equivalent: <code>nova redact {capsulePath}</code>
            </p>
            <button
              onClick={onRunRedact}
              className="px-3 py-1.5 text-xs rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] font-medium"
            >
              Run redact now
            </button>
          </>
        )}
      </div>
    );
  }

  if (!proof) return null;

  const totalFindings = proof.findings_count.total;
  const isClean = totalFindings === 0;
  const bySev = proof.findings_count.by_severity;

  return (
    <div className="space-y-4">
      {/* Summary header */}
      <div className={clsx(
        'rounded-lg border p-4',
        isClean
          ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)]'
          : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)]',
      )}>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <span className={clsx(
              'text-base font-mono font-semibold',
              isClean ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
            )}>
              {isClean ? '✓ Clean' : `⚠ ${totalFindings} finding${totalFindings !== 1 ? 's' : ''}`}
            </span>
            {!isClean && (
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {(['critical', 'high', 'medium', 'low', 'info'] as const).map(s =>
                  (bySev[s] ?? 0) > 0
                    ? (
                      <span
                        key={s}
                        className={`inline-block font-mono text-[var(--text-2xs)] uppercase px-1.5 py-px rounded border ${SEVERITY_STYLE[s] ?? SEVERITY_STYLE.info}`}
                      >
                        {s} ×{bySev[s]}
                      </span>
                    )
                    : null
                )}
              </div>
            )}
          </div>
          <div className="text-right text-[10px] font-mono text-[var(--color-text-faint)] space-y-0.5">
            <div>{proof.bytes_scanned.toLocaleString()} B scanned</div>
            <div>{proof.bytes_redacted.toLocaleString()} B redacted</div>
          </div>
        </div>
        <div className="mt-2 text-[var(--text-2xs)] font-mono text-[var(--color-text-faint)] space-y-0.5">
          <div>proof_id: {proof.proof_id}  ·  {proof.created_at.slice(0, 16)}</div>
          <div className="truncate">chain: {proof.chain_hash}</div>
        </div>
      </div>

      {/* Scanner + packs */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Scanner</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">name</dt>
          <dd className="text-[var(--color-text-muted)]">{proof.scanner.name} v{proof.scanner.version}</dd>
          <dt className="text-[var(--color-text-faint)]">engine</dt>
          <dd className="text-[var(--color-text-muted)]">{proof.scanner.engine} v{proof.scanner.engine_version}</dd>
        </dl>
        {proof.packs.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {proof.packs.map(p => (
              <span key={p.name} className="text-[var(--text-2xs)] font-mono px-1.5 py-px rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)]">
                {p.name} v{p.version} ({p.rules_count} rules)
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Targets */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">
          Targets ({proof.targets.length})
        </h4>
        <div className="overflow-x-auto">
          <table className="w-full text-[10px] font-mono text-[var(--color-text-muted)]">
            <thead>
              <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                <th className="text-left pb-1.5 pr-3">kind</th>
                <th className="text-left pb-1.5 pr-3">ref</th>
                <th className="text-right pb-1.5 pr-3">bytes</th>
                <th className="text-right pb-1.5 pr-3">findings</th>
                <th className="text-center pb-1.5">hash status</th>
              </tr>
            </thead>
            <tbody>
              {proof.targets.map(t => {
                const dirty = t.hash_before_redaction !== t.hash_after_redaction;
                return (
                  <tr key={t.ref} className="border-b border-[var(--color-border)] last:border-0">
                    <td className="py-1.5 pr-3 text-[var(--color-text-faint)]">{t.kind}</td>
                    <td className="py-1.5 pr-3">{t.ref}</td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">{t.bytes_scanned.toLocaleString()}</td>
                    <td className={clsx('py-1.5 pr-3 text-right tabular-nums', t.findings_count > 0 ? 'text-[var(--color-status-failure)]' : '')}>
                      {t.findings_count}
                    </td>
                    <td className="py-1.5 text-center">
                      {t.skipped
                        ? <span className="text-[var(--color-text-faint)]">skipped</span>
                        : dirty
                        ? <span className="text-[var(--color-status-pending)]">redacted</span>
                        : <span className="text-[var(--color-status-success)]">✓ clean</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Findings detail */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">
          Findings ({totalFindings})
        </h4>
        {totalFindings === 0 ? (
          <p className="text-xs text-[var(--color-text-muted)]">No secrets detected in this capsule.</p>
        ) : (
          <ul className="space-y-3">
            {proof.findings.map(f => (
              <li key={f.finding_id} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 space-y-1.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <SeverityBadge severity={f.severity} />
                  <code className="text-xs text-[var(--color-text)]">{f.rule_id}</code>
                  <span className="text-[10px] text-[var(--color-text-faint)] font-mono ml-auto">{f.pack}</span>
                </div>
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[10px] font-mono text-[var(--color-text-muted)]">
                  <dt className="text-[var(--color-text-faint)]">target</dt>
                  <dd>{f.target_ref} @ offset {f.byte_offset} ({f.byte_length}B)</dd>
                  <dt className="text-[var(--color-text-faint)]">match_hash</dt>
                  <dd className="truncate">{f.match_hash}</dd>
                  <dt className="text-[var(--color-text-faint)]">strategy</dt>
                  <dd>{f.redaction_strategy} → <code className="text-[var(--color-text-faint)]">{f.replacement}</code></dd>
                </dl>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Re-run action */}
      <div className="flex justify-end">
        <button
          onClick={onRunRedact}
          className="px-3 py-1.5 text-[10px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] font-mono uppercase tracking-wider"
        >
          Re-scan &amp; rewrite proof
        </button>
      </div>
    </div>
  );
}
