import { useEffect, useRef } from 'react';
import { clsx } from 'clsx';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: string;
  cliEquivalent: string;
  /** Optional summary block (e.g. file diff, eval suite list). */
  details?: React.ReactNode;
  /** Visual tone — destructive actions in v0.8 should still feel measured. */
  tone?: 'default' | 'destructive';
  /** Dialog width — defaults to 'md' (448px). Use 'lg' when details need more space. */
  size?: 'md' | 'lg';
  confirmLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmDialog({
  open,
  title,
  description,
  cliEquivalent,
  details,
  tone = 'default',
  size = 'md',
  confirmLabel = 'Confirm',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  // Keep a stable ref to onCancel so the keydown effect doesn't re-run (and steal
  // focus) every time the parent re-renders with a new inline arrow function.
  const onCancelRef = useRef(onCancel);
  useEffect(() => { onCancelRef.current = onCancel; });

  // Keep a stable ref so the keydown handler always reads the latest busy value
  // without needing to re-register the listener on every busy change.
  const busyRef = useRef(busy);
  useEffect(() => { busyRef.current = busy; });

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busyRef.current) onCancelRef.current();
    };
    window.addEventListener('keydown', onKey);
    // Only steal focus once — when the dialog first opens — never on busy toggle.
    cancelRef.current?.focus();
    return () => { window.removeEventListener('keydown', onKey); };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!open) return null;

  return (
    <>
      {/* Blur backdrop — pointer-events-none avoids Chromium/Linux compositing
          issues where backdrop-filter on a fixed ancestor swallows child events. */}
      <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm pointer-events-none" aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-desc"
        className="fixed inset-0 z-50 flex items-center justify-center p-4"
        onClick={(e) => { if (e.target === e.currentTarget && !busy) onCancel(); }}
      >
      <div className={clsx('w-full rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] shadow-2xl shadow-black/50', size === 'lg' ? 'max-w-lg' : 'max-w-md')} onClick={(e) => e.stopPropagation()}>
        <header className="px-5 py-4 border-b border-[var(--color-border)]">
          <h2 id="confirm-title" className="text-base font-medium text-[var(--color-text)]">{title}</h2>
        </header>

        <div className="px-5 py-4 space-y-3">
          <p id="confirm-desc" className="text-sm text-[var(--color-text-muted)] leading-relaxed">{description}</p>

          {details && (
            <div className="text-xs">
              {details}
            </div>
          )}

          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3">
            <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Equivalent CLI command</p>
            <code className="block text-xs font-mono text-[var(--color-text)] break-all">$ {cliEquivalent}</code>
          </div>

          <p className="text-[10px] text-[var(--color-text-faint)]">
            This action will be appended to <code className="font-mono">~/.novafabric/dashboard-audit.jsonl</code>.
          </p>
        </div>

        <footer className="px-5 py-3 border-t border-[var(--color-border)] flex items-center justify-end gap-2 bg-[var(--color-bg-sunken)]">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="px-3 py-1.5 rounded text-xs font-medium border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={clsx(
              'px-3 py-1.5 rounded text-xs font-medium transition-colors',
              tone === 'destructive'
                ? 'bg-[var(--color-status-failure)] text-white hover:opacity-90'
                : 'bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)]',
              'disabled:opacity-60',
            )}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </footer>
      </div>
    </div>
    </>
  );
}
