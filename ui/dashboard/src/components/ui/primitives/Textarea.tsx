/**
 * Textarea primitive with the shared control chrome.
 */
import { forwardRef, type TextareaHTMLAttributes } from 'react';
import { clsx } from 'clsx';

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & {
  invalid?: boolean;
};

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid = false, className, ...rest },
  ref,
) {
  return (
    <textarea
      ref={ref}
      className={clsx(
        'w-full px-2 py-1.5 rounded border bg-[var(--color-bg-sunken)] text-xs text-[var(--color-text)]',
        'placeholder:text-[var(--color-text-faint)] font-mono leading-relaxed',
        'transition-colors duration-[var(--duration-fast)]',
        'focus:outline-none focus:border-[var(--color-accent)] disabled:opacity-50',
        invalid
          ? 'border-[color-mix(in_oklab,var(--color-status-failure)_50%,transparent)]'
          : 'border-[var(--color-border)]',
        className,
      )}
      {...rest}
    />
  );
});

export default Textarea;
