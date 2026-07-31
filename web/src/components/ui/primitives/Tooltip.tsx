/**
 * Lightweight CSS tooltip (no positioning lib) for icon buttons and truncated
 * text. For rich content use a popover pattern instead.
 */
import type { ReactNode } from 'react';
import { clsx } from 'clsx';

export interface TooltipProps {
  label: string;
  side?: 'top' | 'bottom';
  className?: string;
  children: ReactNode;
}

export default function Tooltip({ label, side = 'top', className, children }: TooltipProps) {
  return (
    <span className={clsx('relative inline-flex group/tip', className)}>
      {children}
      <span
        role="tooltip"
        className={clsx(
          'pointer-events-none absolute left-1/2 -translate-x-1/2 z-[70] whitespace-nowrap',
          'rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-1.5 py-0.5',
          'text-[var(--text-2xs)] text-[var(--color-text)] shadow-[var(--shadow-1)]',
          'opacity-0 group-hover/tip:opacity-100 group-focus-within/tip:opacity-100',
          'transition-opacity duration-[var(--duration-fast)]',
          side === 'top' ? 'bottom-full mb-1' : 'top-full mt-1',
        )}
      >
        {label}
      </span>
    </span>
  );
}
