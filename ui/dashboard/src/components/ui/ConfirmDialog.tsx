/**
 * Modal confirmation gate for destructive or state-changing actions.
 *
 * Used by ActionButton (via its `confirm` prop) and directly for
 * RBAC-sensitive operations like rebuild-metadata-db and daemon control,
 * per the "safe mutations only" decision — these are never one-click.
 */
import Button from './primitives/Button';
import Modal from './primitives/Modal';

interface ConfirmDialogProps {
  title: string;
  body?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: 'default' | 'danger';
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmDialog({
  title,
  body,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'default',
  pending = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal
      title={title}
      onClose={onCancel}
      locked={pending}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            variant={tone === 'danger' ? 'danger' : 'primary'}
            onClick={onConfirm}
            pending={pending}
            autoFocus
          >
            {pending ? 'Working…' : confirmLabel}
          </Button>
        </>
      }
    >
      {body ? (
        <div className="text-sm text-[var(--color-text-muted)] space-y-2">{body}</div>
      ) : null}
    </Modal>
  );
}
