/**
 * Uniform chrome for a dashboard tab: real page header (icon + title +
 * subtitle), a help affordance, an actions toolbar, an optional sub-navigation
 * slot, and consistent spacing. Gives every tab the same header layout so new
 * tabs look native without re-implementing chrome.
 */
import { useState, type ReactNode } from 'react';
import Icon, { type IconName } from '../../ui/primitives/Icon';

interface TabShellProps {
  title: string;
  subtitle?: ReactNode;
  /** Semantic icon shown beside the title. */
  icon?: IconName;
  /** Short help text shown when the user clicks the (?) affordance. */
  help?: ReactNode;
  /** Right-aligned controls (buttons, filters). */
  actions?: ReactNode;
  /** The CLI command(s) this tab mirrors — shown in the help popover. */
  cli?: string | string[];
  /** Sub-navigation (SegmentedControl) rendered under the header. */
  subnav?: ReactNode;
  children: ReactNode;
}

export default function TabShell({
  title,
  subtitle,
  icon,
  help,
  actions,
  cli,
  subnav,
  children,
}: TabShellProps) {
  const [showHelp, setShowHelp] = useState(false);
  const cliList = cli ? (Array.isArray(cli) ? cli : [cli]) : [];

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            {icon && (
              <span
                aria-hidden="true"
                className="w-6 h-6 rounded flex items-center justify-center bg-[var(--color-accent-tint)] text-[var(--color-accent)] shrink-0"
              >
                <Icon name={icon} size={14} />
              </span>
            )}
            <h2 className="text-base font-semibold text-[var(--color-text)] tracking-tight">{title}</h2>
            {(help || cliList.length > 0) && (
              <button
                type="button"
                onClick={() => setShowHelp((v) => !v)}
                title="What is this?"
                aria-expanded={showHelp}
                className="w-4 h-4 flex items-center justify-center rounded-full border border-[var(--color-border)] text-2xs text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] transition-colors"
              >
                ?
              </button>
            )}
          </div>
          {subtitle && <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{subtitle}</p>}
        </div>
        {actions && <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">{actions}</div>}
      </div>

      {showHelp && (help || cliList.length > 0) && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-4 py-3 text-xs text-[var(--color-text-muted)] space-y-2">
          {help && <div>{help}</div>}
          {cliList.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[var(--color-text-faint)]">CLI:</span>
              {cliList.map((c) => (
                <code
                  key={c}
                  className="px-1.5 py-0.5 rounded bg-[var(--color-bg-raised)] font-mono text-[var(--color-text)]"
                >
                  {c}
                </code>
              ))}
            </div>
          )}
        </div>
      )}

      {subnav}

      {children}
    </div>
  );
}
