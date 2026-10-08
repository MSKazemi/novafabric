// Shared chrome for the Admin tab panels — section heading, panel card,
// confirm dialog, CLI-reference row. Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { clsx } from 'clsx';
import CopyButton from '../../../ui/CopyButton';

export function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-[11px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] mb-3">
      {children}
    </h2>
  );
}

export function Panel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={clsx(
        'rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4',
        className,
      )}
    >
      {children}
    </div>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
  danger,
}: {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  danger?: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-sm rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5 shadow-xl">
        <h3 className="text-sm font-semibold text-[var(--color-text)] mb-2">{title}</h3>
        <p className="text-xs text-[var(--color-text-muted)] mb-5">{message}</p>
        <div className="flex gap-2 justify-end">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded text-xs font-medium border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className={clsx(
              'px-3 py-1.5 rounded text-xs font-medium transition-colors',
              danger
                ? 'bg-[var(--color-status-failure)] text-white hover:opacity-90'
                : 'bg-[var(--color-accent)] text-white hover:opacity-90',
            )}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function CliRefRow({ cmd, label }: { cmd: string; label: string }) {
  return (
    <div className="space-y-1">
      <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">{label}</p>
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 pr-16">
        <code className="text-[11px] font-mono text-[var(--color-text-muted)] break-all">{cmd}</code>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cmd} label="copy" />
        </div>
      </div>
    </div>
  );
}
