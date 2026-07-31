/**
 * Raised surface card with optional header/footer slots and collapse.
 */
import { useState, type ReactNode } from 'react';
import { clsx } from 'clsx';
import Icon from './Icon';

export interface CardProps {
  title?: ReactNode;
  /** Small mono annotation next to the title (counts, ids). */
  meta?: ReactNode;
  /** Right side of the header (buttons, filters). */
  actions?: ReactNode;
  footer?: ReactNode;
  collapsible?: boolean;
  defaultCollapsed?: boolean;
  /** Remove inner body padding (for tables/charts that fill the card). */
  flush?: boolean;
  className?: string;
  children: ReactNode;
}

export default function Card({
  title,
  meta,
  actions,
  footer,
  collapsible = false,
  defaultCollapsed = false,
  flush = false,
  className,
  children,
}: CardProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const hasHeader = title !== undefined || actions !== undefined;

  return (
    <section
      className={clsx(
        'rounded-md border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-[var(--shadow-1)]',
        className,
      )}
    >
      {hasHeader && (
        <header className="flex items-center justify-between gap-3 px-4 h-10 border-b border-[var(--color-border)]">
          <div className="flex items-baseline gap-2 min-w-0">
            {collapsible ? (
              <button
                type="button"
                onClick={() => setCollapsed((v) => !v)}
                aria-expanded={!collapsed}
                className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text)] hover:text-[var(--color-accent)] transition-colors min-w-0"
              >
                <Icon
                  name="chevron-down"
                  size={12}
                  className={clsx('shrink-0 transition-transform duration-[var(--duration-fast)]', collapsed && '-rotate-90')}
                />
                <span className="truncate">{title}</span>
              </button>
            ) : (
              <h3 className="text-xs font-semibold text-[var(--color-text)] truncate">{title}</h3>
            )}
            {meta && (
              <span className="text-[var(--text-2xs)] font-mono text-[var(--color-text-faint)] tabular-nums shrink-0">
                {meta}
              </span>
            )}
          </div>
          {actions && <div className="flex items-center gap-1.5 shrink-0">{actions}</div>}
        </header>
      )}
      {!collapsed && <div className={flush ? undefined : 'p-4'}>{children}</div>}
      {!collapsed && footer && (
        <footer className="px-4 py-2 border-t border-[var(--color-border)] text-[var(--text-2xs)] text-[var(--color-text-faint)]">
          {footer}
        </footer>
      )}
    </section>
  );
}
