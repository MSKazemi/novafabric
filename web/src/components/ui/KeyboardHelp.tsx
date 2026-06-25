import type { ReactNode } from 'react';

interface Shortcut { key: string; description: ReactNode; }

const BASE_SHORTCUTS: Shortcut[] = [
  { key: 'j / k',  description: 'Move selection down / up in the current list' },
  { key: 'Enter',  description: 'Open selected item' },
  { key: 'Escape', description: 'Close detail panel / dismiss overlay' },
  { key: '?',      description: 'Toggle this keyboard shortcuts reference' },
  { key: 'r',      description: 'Refresh current tab' },
];

interface KeyboardHelpProps {
  onClose: () => void;
  tabLabels?: string[];
}

export default function KeyboardHelp({ onClose, tabLabels }: KeyboardHelpProps) {
  const tabShortcut: Shortcut = tabLabels && tabLabels.length > 0
    ? {
        key: `1 – ${tabLabels.length}`,
        description: (
          <>
            Switch tab:{' '}
            <span className="text-[var(--color-text)]">
              {tabLabels.slice(0, 6).join(', ')}{tabLabels.length > 6 ? `, … (${tabLabels.length} total)` : ''}
            </span>
          </>
        ),
      }
    : { key: '1 – n', description: 'Switch to tab by number' };

  const shortcuts: Shortcut[] = [tabShortcut, ...BASE_SHORTCUTS];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="absolute inset-0 bg-[var(--color-bg)] opacity-80" aria-hidden="true" />
      <div className="relative w-full max-w-sm rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]">
          <h2 className="text-sm font-medium text-[var(--color-text)]">Keyboard shortcuts</h2>
          <button onClick={onClose} className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none" aria-label="Close">×</button>
        </div>
        <ul className="divide-y divide-[var(--color-border)] px-1 py-1">
          {shortcuts.map(({ key, description }) => (
            <li key={key} className="flex items-center gap-4 px-4 py-2.5">
              <kbd className="font-mono text-xs px-2 py-1 rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-text)] shrink-0 min-w-[3.5rem] text-center">
                {key}
              </kbd>
              <span className="text-xs text-[var(--color-text-muted)]">{description}</span>
            </li>
          ))}
        </ul>
        <div className="px-5 py-3 border-t border-[var(--color-border)]">
          <p className="text-[10px] text-[var(--color-text-faint)]">
            Press <kbd className="font-mono px-1 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">?</kbd> or <kbd className="font-mono px-1 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">Esc</kbd> to close
          </p>
        </div>
      </div>
    </div>
  );
}
