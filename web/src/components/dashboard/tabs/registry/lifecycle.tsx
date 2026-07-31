/**
 * Asset lifecycle helpers + status badge shared across the Registry tab split.
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 */
import { clsx } from 'clsx';

export function nextStatusFor(current: string): string {
  if (current === 'development') return 'staging';
  if (current === 'staging') return 'production';
  if (current === 'production') return 'archived';
  return 'staging';
}

export function validTargetsFor(current: string): string[] {
  if (current === 'development') return ['staging', 'archived'];
  if (current === 'staging') return ['production', 'archived'];
  if (current === 'production') return ['archived'];
  if (current === 'archived') return ['staging'];
  return ['staging'];
}

export function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    development: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]',
    staging: 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_12%,transparent)]',
    production: 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)]',
    archived: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)] opacity-60',
  };
  return (
    <span className={clsx('text-[var(--text-2xs)] uppercase tracking-wider px-1.5 py-px rounded font-medium', colors[status] ?? colors.development)}>
      {status}
    </span>
  );
}
