/**
 * Modal primitive: scrim, focus trap, Esc-to-close, aria-modal.
 * ConfirmDialog and Drawer build on the same trap logic.
 */
import { useEffect, useRef, type ReactNode } from 'react';
import { clsx } from 'clsx';

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function useFocusTrap(active: boolean, onClose?: () => void) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    const previouslyFocused = document.activeElement as HTMLElement | null;

    // Focus the first focusable element inside the dialog.
    const first = container?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    function onKey(e: globalThis.KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose?.();
        return;
      }
      if (e.key !== 'Tab' || !container) return;
      const focusables = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusables.length === 0) return;
      const firstEl = focusables[0]!;
      const lastEl = focusables[focusables.length - 1]!;
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    }

    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      previouslyFocused?.focus?.();
    };
  }, [active, onClose]);

  return containerRef;
}

export interface ModalProps {
  title?: ReactNode;
  onClose: () => void;
  /** Disable scrim-click / Esc close (e.g. while a mutation is pending). */
  locked?: boolean;
  footer?: ReactNode;
  widthClass?: string;
  children: ReactNode;
}

export default function Modal({
  title,
  onClose,
  locked = false,
  footer,
  widthClass = 'max-w-md',
  children,
}: ModalProps) {
  const close = locked ? undefined : onClose;
  const containerRef = useFocusTrap(true, close);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-[var(--color-bg-overlay)] p-4"
      role="dialog"
      aria-modal="true"
      aria-label={typeof title === 'string' ? title : undefined}
      onClick={() => close?.()}
    >
      <div
        ref={containerRef}
        className={clsx(
          'w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-[var(--shadow-2)]',
          widthClass,
        )}
        onClick={(e) => e.stopPropagation()}
      >
        {title !== undefined && (
          <div className="px-5 py-4 border-b border-[var(--color-border)]">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">{title}</h3>
          </div>
        )}
        <div className="px-5 py-4">{children}</div>
        {footer && (
          <div className="px-5 py-3 flex items-center justify-end gap-2 border-t border-[var(--color-border)]">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
