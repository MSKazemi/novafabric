/**
 * Text input primitive with the shared control chrome.
 */
import { forwardRef, type InputHTMLAttributes } from 'react';
import { clsx } from 'clsx';

export type InputProps = InputHTMLAttributes<HTMLInputElement> & {
  invalid?: boolean;
};

export const CONTROL_CLASSES =
  'w-full h-7 px-2 rounded border bg-[var(--color-bg-sunken)] text-xs text-[var(--color-text)] ' +
  'placeholder:text-[var(--color-text-faint)] transition-colors duration-[var(--duration-fast)] ' +
  'focus:outline-none focus:border-[var(--color-accent)] disabled:opacity-50';

const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid = false, className, ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      className={clsx(
        CONTROL_CLASSES,
        invalid
          ? 'border-[color-mix(in_oklab,var(--color-status-failure)_50%,transparent)]'
          : 'border-[var(--color-border)]',
        className,
      )}
      {...rest}
    />
  );
});

export default Input;
