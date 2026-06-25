import type { ReactNode } from 'react';

interface EmptyStateProps {
  message: ReactNode;
  hint?: ReactNode;
  cliCommand?: string;
  icon?: ReactNode;
  variant?: 'bordered' | 'fill' | 'inline';
  className?: string;
}

export default function EmptyState({
  message,
  hint,
  cliCommand,
  icon,
  variant = 'bordered',
  className = '',
}: EmptyStateProps) {
  const resolvedHint: ReactNode = hint ?? (
    cliCommand
      ? (
        <>
          Run{' '}
          <code className="px-1.5 py-0.5 rounded bg-[var(--color-bg-raised)] font-mono text-[var(--color-text)]">
            {cliCommand}
          </code>
        </>
      )
      : null
  );

  const iconEl = icon ? (
    <div className="w-10 h-10 mx-auto mb-3 rounded-lg border border-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] bg-[color-mix(in_oklab,var(--color-accent)_8%,transparent)] flex items-center justify-center text-lg select-none" aria-hidden="true">
      {icon}
    </div>
  ) : null;

  const inner = (
    <>
      {iconEl}
      <p className="text-sm text-[var(--color-text-muted)]">{message}</p>
      {resolvedHint && (
        <p className="mt-2 text-xs text-[var(--color-text-faint)]">{resolvedHint}</p>
      )}
    </>
  );

  if (variant === 'inline') {
    return <div className={`p-4 text-center ${className}`}>{inner}</div>;
  }

  if (variant === 'fill') {
    return (
      <div className={`flex flex-col items-center justify-center h-full gap-1 text-center px-6 ${className}`}>
        {inner}
      </div>
    );
  }

  // bordered (default)
  return (
    <div className={`rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-8 text-center ${className}`}>
      {inner}
    </div>
  );
}
