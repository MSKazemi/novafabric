/**
 * Labeled form field: label + control + optional description / error.
 * Wires aria-describedby so screen readers announce errors.
 */
import { useId, type ReactNode } from 'react';
import { clsx } from 'clsx';

export interface FieldProps {
  label: ReactNode;
  required?: boolean;
  description?: ReactNode;
  error?: string | null;
  /** Render-prop so the control can receive the generated ids. */
  children: (ids: { id: string; describedBy?: string }) => ReactNode;
  className?: string;
}

export default function Field({
  label,
  required = false,
  description,
  error,
  children,
  className,
}: FieldProps) {
  const id = useId();
  const descId = description || error ? `${id}-desc` : undefined;

  return (
    <div className={clsx('space-y-1', className)}>
      <label
        htmlFor={id}
        className="block text-2xs font-medium uppercase tracking-wider text-[var(--color-text-faint)]"
      >
        {label}
        {required && <span className="text-[var(--color-status-failure)] ml-0.5">*</span>}
      </label>
      {children({ id, describedBy: descId })}
      {error ? (
        <p id={descId} className="text-2xs text-[var(--color-status-failure)]">
          {error}
        </p>
      ) : (
        description && (
          <p id={descId} className="text-2xs text-[var(--color-text-faint)]">
            {description}
          </p>
        )
      )}
    </div>
  );
}
