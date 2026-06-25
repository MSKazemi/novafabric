import { useState, useCallback } from 'react';
import { clsx } from 'clsx';

interface CopyButtonProps {
  text: string;
  className?: string;
  label?: string;
}

export default function CopyButton({ text, className, label = 'Copy' }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // older browsers — graceful no-op
    }
  }, [text]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={copied ? 'Copied to clipboard' : `${label} ${text}`}
      className={clsx(
        'inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs font-medium transition-colors',
        'border border-[var(--color-border)] hover:border-[var(--color-border-strong)]',
        'text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
        'bg-[var(--color-bg-raised)] hover:bg-[var(--color-bg)]',
        className,
      )}
    >
      {copied ? (
        <>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M2 6.5L4.5 9L10 3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Copied
        </>
      ) : (
        <>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <rect x="3" y="3" width="6.5" height="6.5" rx="1" stroke="currentColor" strokeWidth="1.4" />
            <path d="M2 7.5V2.5C2 1.94772 2.44772 1.5 3 1.5H8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
          {label}
        </>
      )}
    </button>
  );
}
