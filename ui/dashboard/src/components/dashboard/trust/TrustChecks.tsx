/**
 * Trust Center — per-check verification list for one run (issue #271).
 *
 * Renders `buildTrustChecks` (see ./checks.ts): every check `nova verify`
 * performs, each with its status in words, the reason, what it looked at, and
 * the `nova verify` command an auditor runs offline to check it independently.
 * The status is always text (icons are decorative), the list is an ordered
 * list of labelled articles, and the verdict never reads stronger than the
 * weakest check.
 */
import { useId, useState } from 'react';
import { clsx } from 'clsx';
import type { CapsuleVerifyResult } from '../../../lib/api';
import CopyButton from '../../ui/CopyButton';
import {
  STATUS_WORDS,
  buildTrustChecks,
  trustSummary,
  trustVerdict,
  type TrustStatus,
} from './checks';

const STATUS_ICON: Record<TrustStatus, string> = {
  passed: '✓',
  failed: '✗',
  not_established: '?',
  not_applicable: '–',
};

const STATUS_TEXT: Record<TrustStatus, string> = {
  passed: 'text-[var(--color-status-success)]',
  failed: 'text-[var(--color-status-failure)]',
  not_established: 'text-[var(--color-status-pending)]',
  not_applicable: 'text-[var(--color-text-faint)]',
};

const VERDICT_TEXT = {
  passed: 'text-[var(--color-status-success)]',
  failed: 'text-[var(--color-status-failure)]',
  partial: 'text-[var(--color-status-pending)]',
  neutral: 'text-[var(--color-text-muted)]',
} as const;

function CopySummary({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // No clipboard (older browser, insecure context): the list stays readable.
    }
  }
  return (
    <button
      type="button"
      onClick={copy}
      className="text-xs px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
    >
      {copied ? 'Summary copied' : 'Copy summary'}
    </button>
  );
}

export default function TrustChecks({
  result,
  runId,
  capsulePath,
}: {
  result: CapsuleVerifyResult;
  runId: string;
  capsulePath?: string | null;
}) {
  const headingId = useId();
  const checks = buildTrustChecks(result, capsulePath);
  const verdict = trustVerdict(result, checks);

  return (
    <section
      aria-labelledby={headingId}
      data-testid="trust-checks"
      className="rounded border border-[var(--color-border)] bg-[var(--color-bg)] p-3 space-y-3"
    >
      <header className="flex items-start justify-between gap-3 flex-wrap">
        <div className="space-y-1 min-w-0">
          <h4 id={headingId} className="text-xs font-medium text-[var(--color-text)]">
            Every check, one by one
          </h4>
          <p data-testid="trust-verdict" className={clsx('text-xs font-medium', VERDICT_TEXT[verdict.tone])}>
            {verdict.headline}
          </p>
          <p className="text-[10px] text-[var(--color-text-muted)]">{verdict.detail}</p>
        </div>
        <CopySummary text={trustSummary(runId, result, checks, capsulePath)} />
      </header>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        {result.configured
          ? 'The dashboard server checked this capsule with this host’s novaseal.yaml (its logs and trust anchors).'
          : 'The dashboard server checked this capsule from its own files: no novaseal.yaml, local log or trust anchor on this host.'}{' '}
        To check it independently, copy the capsule directory to a machine you trust and run the command
        shown with each check. <code className="font-mono">nova verify</code> needs no server and no network;
        replace each <code className="font-mono">&lt;…&gt;</code> with your own file.
      </p>

      <ol className="space-y-2" aria-label="Verification checks">
        {checks.map((c) => {
          const nameId = `${headingId}-${c.id}`;
          return (
            <li key={c.id}>
              <article
                aria-labelledby={nameId}
                data-testid={`trust-check-${c.id}`}
                data-status={c.status}
                className="rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-2.5 space-y-1.5"
              >
                <div className="flex items-baseline justify-between gap-2 flex-wrap">
                  <h5 id={nameId} className="text-xs text-[var(--color-text)]">{c.name}</h5>
                  <span className={clsx('text-xs font-medium whitespace-nowrap', STATUS_TEXT[c.status])}>
                    <span aria-hidden="true">{STATUS_ICON[c.status]} </span>
                    {STATUS_WORDS[c.status]}
                  </span>
                </div>
                <dl className="grid grid-cols-1 sm:grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-[11px]">
                  <dt className="text-[var(--color-text-faint)]">Reason</dt>
                  <dd className="text-[var(--color-text-muted)] break-words">{c.reason}</dd>
                  <dt className="text-[var(--color-text-faint)]">Looked at</dt>
                  <dd className="text-[var(--color-text-muted)] min-w-0">
                    <ul className="list-disc pl-4 space-y-0.5">
                      {c.evidence.map((e, i) => (
                        <li key={i} className="break-all font-mono text-[10px]">{e}</li>
                      ))}
                    </ul>
                  </dd>
                  <dt className="text-[var(--color-text-faint)]">Check it yourself</dt>
                  <dd className="flex items-start gap-2 min-w-0">
                    <code className="flex-1 min-w-0 break-all font-mono text-[10px] text-[var(--color-text)] bg-[var(--color-bg-sunken)] rounded px-1.5 py-1">
                      {c.command}
                    </code>
                    <CopyButton text={c.command} label="Copy" />
                  </dd>
                </dl>
              </article>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
