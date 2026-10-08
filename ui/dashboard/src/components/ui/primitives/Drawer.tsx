/**
 * Right-side drawer for detail inspection (run details, key details) —
 * same trap/scrim semantics as Modal, panel anchored to the edge.
 */
import type { ReactNode } from 'react';
import { clsx } from 'clsx';
import Icon from './Icon';
import { useFocusTrap } from './Modal';

export interface DrawerProps {
  title?: ReactNode;
  onClose: () => void;
  widthClass?: string;
  children: ReactNode;
}

export default function Drawer({ title, onClose, widthClass = 'max-w-xl', children }: DrawerProps) {
  const containerRef = useFocusTrap(true, onClose);

  return (
    <div
      className="fixed inset-0 z-[60] flex justify-end bg-[var(--color-bg-overlay)]"
      role="dialog"
      aria-modal="true"
      aria-label={typeof title === 'string' ? title : undefined}
      onClick={onClose}
    >
      <div
        ref={containerRef}
        className={clsx(
          'h-full w-full border-l border-[var(--color-border)] bg-[var(--color-bg)] shadow-[var(--shadow-2)] flex flex-col',
          widthClass,
        )}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 px-4 h-11 border-b border-[var(--color-border)] shrink-0">
          <h3 className="text-sm font-semibold text-[var(--color-text)] truncate">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="w-6 h-6 flex items-center justify-center rounded text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)] transition-colors"
          >
            <Icon name="close" size={14} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  );
}
