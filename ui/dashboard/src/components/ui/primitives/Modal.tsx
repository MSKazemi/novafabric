/**
 * Modal primitive: scrim, focus trap, Esc-to-close, aria-modal.
 * ConfirmDialog and Drawer build on the same trap logic.
 */
import { useEffect, useId, useRef, type ReactNode } from 'react';
import { clsx } from 'clsx';

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Tab-reachable elements inside `container`. `button:not([disabled])` alone also
 * matches a button with tabindex="-1" (e.g. a listbox option), and treating that
 * as the wrap-around edge let Tab escape the dialog — so tabindex=-1 is excluded.
 */
export function tabbableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.getAttribute('tabindex') !== '-1',
  );
}

/**
 * Open traps, innermost last. Only the innermost trap handles Escape and Tab, so
 * Escape in a confirm dialog opened from another dialog closes just the top one.
 * React runs a child's effects before its parent's, so a dialog rendered *inside*
 * another registers first; an ancestor is therefore inserted below any trap it
 * contains instead of being pushed on top.
 */
interface TrapEntry { el: HTMLElement | null }
const trapStack: TrapEntry[] = [];

function registerTrap(entry: TrapEntry): boolean {
  const i = entry.el ? trapStack.findIndex((t) => t.el !== null && entry.el!.contains(t.el)) : -1;
  if (i === -1) { trapStack.push(entry); return true; }
  trapStack.splice(i, 0, entry);
  return false; // a nested dialog is already on top and owns focus
}

export function useFocusTrap(active: boolean, onClose?: () => void) {
  const containerRef = useRef<HTMLDivElement>(null);
  // Read onClose through a ref: callers pass inline arrows, and re-running the
  // effect on every parent render restored focus to the trigger and then yanked
  // it back to the first control — the user lost their place on each re-render.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    const token: TrapEntry = { el: container };
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const onTop = registerTrap(token);

    // Focus the first focusable element inside the dialog, or the dialog itself.
    if (onTop) {
      const first = container ? tabbableIn(container)[0] : undefined;
      if (first) first.focus();
      else container?.focus();
    }

    function onKey(e: globalThis.KeyboardEvent) {
      if (trapStack[trapStack.length - 1] !== token) return;
      if (e.key === 'Escape') {
        onCloseRef.current?.();
        return;
      }
      if (e.key !== 'Tab' || !container) return;
      const focusables = tabbableIn(container);
      if (focusables.length === 0) {
        e.preventDefault();
        return;
      }
      const firstEl = focusables[0]!;
      const lastEl = focusables[focusables.length - 1]!;
      const current = document.activeElement;
      if (!current || !container.contains(current)) {
        e.preventDefault();
        (e.shiftKey ? lastEl : firstEl).focus();
      } else if (e.shiftKey && current === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && current === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    }

    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      const i = trapStack.indexOf(token);
      if (i !== -1) trapStack.splice(i, 1);
      previouslyFocused?.focus?.();
    };
  }, [active]);

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
  const titleId = useId();

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-[var(--color-bg-overlay)] p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby={title !== undefined ? titleId : undefined}
      onClick={() => close?.()}
    >
      <div
        ref={containerRef}
        tabIndex={-1}
        className={clsx(
          'w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-[var(--shadow-2)]',
          widthClass,
        )}
        onClick={(e) => e.stopPropagation()}
      >
        {title !== undefined && (
          <div className="px-5 py-4 border-b border-[var(--color-border)]">
            <h3 id={titleId} className="text-sm font-semibold text-[var(--color-text)]">{title}</h3>
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
