/**
 * Button for triggering a mutation. Shows a pending spinner, disables while
 * in flight, and optionally gates the action behind a ConfirmDialog (used for
 * destructive / RBAC-sensitive operations per the "safe mutations only" rule).
 *
 * Pair with useMutation:
 *   const m = useMutation(api.redact, { successMessage: 'Redacted' });
 *   <ActionButton onClick={() => m.run(runId)} pending={m.pending}>Redact</ActionButton>
 */
import { useState, type ReactNode } from 'react';
import { clsx } from 'clsx';
import ConfirmDialog from './ConfirmDialog';

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
type Size = 'sm' | 'md';

interface ActionButtonProps {
  onClick: () => void | Promise<unknown>;
  children: ReactNode;
  pending?: boolean;
  disabled?: boolean;
  variant?: Variant;
  size?: Size;
  title?: string;
  className?: string;
  /** When set, clicking opens a confirmation dialog before running onClick. */
  confirm?: {
    title: string;
    body?: ReactNode;
    confirmLabel?: string;
    tone?: 'default' | 'danger';
  };
}

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-[var(--color-accent)] text-[var(--color-bg)] hover:opacity-90 border border-transparent',
  secondary:
    'border border-[var(--color-border)] text-[var(--color-text)] hover:border-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_8%,transparent)]',
  danger:
    'border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]',
  ghost: 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] border border-transparent',
};

const SIZES: Record<Size, string> = {
  sm: 'px-2 py-1 text-[11px]',
  md: 'px-3 py-1.5 text-xs',
};

export default function ActionButton({
  onClick,
  children,
  pending = false,
  disabled = false,
  variant = 'secondary',
  size = 'md',
  title,
  className = '',
  confirm,
}: ActionButtonProps) {
  const [confirming, setConfirming] = useState(false);

  const fire = () => {
    setConfirming(false);
    void onClick();
  };

  return (
    <>
      <button
        type="button"
        title={title}
        disabled={disabled || pending}
        onClick={() => (confirm ? setConfirming(true) : fire())}
        className={clsx(
          'inline-flex items-center gap-1.5 rounded font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap',
          VARIANTS[variant],
          SIZES[size],
          className,
        )}
      >
        {pending && (
          <span
            aria-hidden="true"
            className="inline-block w-3 h-3 rounded-full border-2 border-current border-r-transparent animate-spin"
          />
        )}
        {children}
      </button>
      {confirming && (
        <ConfirmDialog
          title={confirm!.title}
          body={confirm!.body}
          confirmLabel={confirm!.confirmLabel}
          tone={confirm!.tone}
          pending={pending}
          onConfirm={fire}
          onCancel={() => setConfirming(false)}
        />
      )}
    </>
  );
}
