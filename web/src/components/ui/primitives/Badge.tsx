/**
 * Small annotation chip — the uppercase micro-label pattern used across the
 * dashboard (nav badges, experimental markers, series legends).
 */
import type { ReactNode } from 'react';
import { clsx } from 'clsx';

export type BadgeTone = 'neutral' | 'accent' | 'success' | 'danger' | 'pending' | 'info';

const TONES: Record<BadgeTone, string> = {
  neutral: 'bg-[var(--color-faint-tint)] text-[var(--color-text-faint)]',
  accent: 'bg-[var(--color-accent-tint)] text-[var(--color-accent)]',
  success: 'bg-[var(--color-success-tint)] text-[var(--color-status-success)]',
  danger: 'bg-[var(--color-danger-tint)] text-[var(--color-status-failure)]',
  pending: 'bg-[var(--color-pending-tint)] text-[var(--color-status-pending)]',
  info: 'bg-[color-mix(in_oklab,var(--color-edge-derived-from)_15%,transparent)] text-[var(--color-edge-derived-from)]',
};

export interface BadgeProps {
  tone?: BadgeTone;
  /** Optional dot before the text. */
  dot?: boolean;
  className?: string;
  children: ReactNode;
}

export default function Badge({ tone = 'neutral', dot = false, className, children }: BadgeProps) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 px-1.5 py-px rounded text-2xs uppercase tracking-wider font-medium whitespace-nowrap',
        TONES[tone],
        className,
      )}
    >
      {dot && <span aria-hidden="true" className="w-1 h-1 rounded-full bg-current shrink-0" />}
      {children}
    </span>
  );
}
