/**
 * Run/asset status indicator — maps raw status strings to the status tokens.
 * Absorbs the StatusDot pattern from dashboard/helpers.tsx with a labeled form.
 */
import { clsx } from 'clsx';

export interface StatusPillProps {
  status: string | null | undefined;
  /** dot = just the colored dot; label = dot + text. */
  variant?: 'dot' | 'label';
  className?: string;
}

function toneFor(status: string | null | undefined): 'success' | 'failure' | 'pending' | 'neutral' {
  switch ((status ?? '').toLowerCase()) {
    case 'success':
    case 'completed':
    case 'passed':
    case 'ok':
    case 'active':
    case 'promoted':
      return 'success';
    case 'failure':
    case 'failed':
    case 'error':
    case 'rejected':
      return 'failure';
    case 'pending':
    case 'running':
    case 'in_progress':
    case 'partially_complete':
      return 'pending';
    default:
      return 'neutral';
  }
}

const DOT: Record<ReturnType<typeof toneFor>, string> = {
  success: 'bg-[var(--color-status-success)]',
  failure: 'bg-[var(--color-status-failure)]',
  pending: 'bg-[var(--color-status-pending)]',
  neutral: 'bg-[var(--color-text-faint)]',
};

const TEXT: Record<ReturnType<typeof toneFor>, string> = {
  success: 'text-[var(--color-status-success)]',
  failure: 'text-[var(--color-status-failure)]',
  pending: 'text-[var(--color-status-pending)]',
  neutral: 'text-[var(--color-text-faint)]',
};

export default function StatusPill({ status, variant = 'label', className }: StatusPillProps) {
  const tone = toneFor(status);
  if (variant === 'dot') {
    return (
      <span
        className={clsx('inline-block w-1.5 h-1.5 rounded-full shrink-0', DOT[tone], className)}
        aria-label={status ?? 'unknown'}
      />
    );
  }
  return (
    <span className={clsx('inline-flex items-center gap-1.5 text-[var(--text-2xs)] font-mono', TEXT[tone], className)}>
      <span aria-hidden="true" className={clsx('w-1.5 h-1.5 rounded-full shrink-0', DOT[tone])} />
      {status ?? 'unknown'}
    </span>
  );
}
