/**
 * The base button primitive. ActionButton (mutation + confirm gating) wraps
 * this; use Button directly for plain interactions.
 */
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { clsx } from 'clsx';
import Icon, { type IconName } from './Icon';

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner and disables the button. */
  pending?: boolean;
  /** Leading icon by semantic name. */
  icon?: IconName;
  children?: ReactNode;
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] border border-transparent shadow-[var(--shadow-1)]',
  secondary:
    'border border-[var(--color-border)] bg-[var(--color-bg-raised)] text-[var(--color-text)] hover:border-[var(--color-border-strong)] hover:bg-[var(--color-surface-hover)]',
  danger:
    'border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] text-[var(--color-status-failure)] hover:bg-[var(--color-danger-tint)]',
  ghost:
    'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)] border border-transparent',
};

const SIZES: Record<ButtonSize, string> = {
  sm: 'h-6 px-2 text-[var(--text-2xs)] gap-1',
  md: 'h-7 px-3 text-xs gap-1.5',
};

const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', pending = false, icon, disabled, className, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled || pending}
      className={clsx(
        'inline-flex items-center justify-center rounded font-medium whitespace-nowrap select-none',
        'transition-colors duration-[var(--duration-fast)]',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {pending ? (
        <Icon name="spinner" size={size === 'sm' ? 12 : 14} className="animate-spin" />
      ) : (
        icon && <Icon name={icon} size={size === 'sm' ? 12 : 14} />
      )}
      {children}
    </button>
  );
});

export default Button;
