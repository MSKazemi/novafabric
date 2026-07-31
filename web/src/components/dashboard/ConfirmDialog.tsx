/**
 * Layer-B confirmation dialog: the ui/ConfirmDialog with the dashboard's
 * mutation contract baked in — description, equivalent CLI command
 * (ADR-0027: every action surfaces its CLI form), optional details block,
 * and the audit-log notice. Thin adapter; the modal itself lives in
 * ui/primitives/Modal via ui/ConfirmDialog.
 */
import BaseConfirmDialog from '../ui/ConfirmDialog';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: string;
  cliEquivalent: string;
  /** Optional summary block (e.g. file diff, eval suite list). */
  details?: React.ReactNode;
  /** Visual tone — destructive actions in v0.8 should still feel measured. */
  tone?: 'default' | 'destructive';
  /** Dialog width — kept for API compatibility; both sizes render max-w-md/lg. */
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
  confirmLabel = 'Confirm',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  if (!open) return null;

  return (
    <BaseConfirmDialog
      title={title}
      tone={tone === 'destructive' ? 'danger' : 'default'}
      confirmLabel={confirmLabel}
      pending={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
      body={
        <>
          <p className="leading-relaxed">{description}</p>
          {details && <div className="text-xs">{details}</div>}
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3">
            <p className="text-[var(--text-2xs)] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">
              Equivalent CLI command
            </p>
            <code className="block text-xs font-mono text-[var(--color-text)] break-all">
              $ {cliEquivalent}
            </code>
          </div>
          <p className="text-[var(--text-2xs)] text-[var(--color-text-faint)]">
            This action will be appended to{' '}
            <code className="font-mono">~/.novafabric/dashboard-audit.jsonl</code>.
          </p>
        </>
      }
    />
  );
}
