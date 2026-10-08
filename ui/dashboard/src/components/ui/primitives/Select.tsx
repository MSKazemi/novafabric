/**
 * Select primitive with the shared control chrome.
 */
import { forwardRef, type SelectHTMLAttributes } from 'react';
import { clsx } from 'clsx';

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement> & {
  invalid?: boolean;
};

const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { invalid = false, className, children, ...rest },
  ref,
) {
  return (
    <select
      ref={ref}
      className={clsx(
        'w-full h-7 px-2 rounded border bg-[var(--color-bg-sunken)] text-xs text-[var(--color-text)]',
        'transition-colors duration-[var(--duration-fast)]',
        'focus:outline-none focus:border-[var(--color-accent)] disabled:opacity-50',
        invalid
          ? 'border-[color-mix(in_oklab,var(--color-status-failure)_50%,transparent)]'
          : 'border-[var(--color-border)]',
        className,
      )}
      {...rest}
    >
      {children}
    </select>
  );
});

export default Select;
