/**
 * Button for triggering a mutation. Shows a pending spinner, disables while
 * in flight, and optionally gates the action behind a ConfirmDialog (used for
 * destructive / RBAC-sensitive operations per the "safe mutations only" rule).
 *
 * Thin wrapper over ui/primitives/Button that adds the confirm flow.
 *
 * Pair with useMutation:
 *   const m = useMutation(api.redact, { successMessage: 'Redacted' });
 *   <ActionButton onClick={() => m.run(runId)} pending={m.pending}>Redact</ActionButton>
 */
import { useState, type ReactNode } from 'react';
import ConfirmDialog from './ConfirmDialog';
import Button, { type ButtonSize, type ButtonVariant } from './primitives/Button';
import type { IconName } from './primitives/Icon';

interface ActionButtonProps {
  onClick: () => void | Promise<unknown>;
  children: ReactNode;
  pending?: boolean;
  disabled?: boolean;
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: IconName;
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

export default function ActionButton({
  onClick,
  children,
  pending = false,
  disabled = false,
  variant = 'secondary',
  size = 'md',
  icon,
  title,
  className,
  confirm,
}: ActionButtonProps) {
  const [confirming, setConfirming] = useState(false);

  const fire = () => {
    setConfirming(false);
    void onClick();
  };

  return (
    <>
      <Button
        title={title}
        disabled={disabled}
        pending={pending}
        variant={variant}
        size={size}
        icon={icon}
        className={className}
        onClick={() => (confirm ? setConfirming(true) : fire())}
      >
        {children}
      </Button>
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
