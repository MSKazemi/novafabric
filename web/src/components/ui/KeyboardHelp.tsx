import type { ReactNode } from 'react';

interface Shortcut { key: string; description: ReactNode; }

const BASE_SHORTCUTS: Shortcut[] = [
  { key: '⌘K / Ctrl+K', description: 'Command palette — jump anywhere, search entities' },
  { key: 'j / k',  description: 'Move selection down / up in the current list' },
  { key: 'Enter',  description: 'Open selected item' },
  { key: 'Escape', description: 'Close detail panel / dismiss overlay' },
  { key: '?',      description: 'Toggle this keyboard shortcuts reference' },
  { key: 'r',      description: 'Refresh current tab' },
];

export interface TabShortcutEntry {
  /** Display sequence, e.g. 'g h'. */
  key: string;
  label: string;
}

interface KeyboardHelpProps {
  onClose: () => void;
  /** Stable per-tab navigation sequences (g-prefixed). */
  tabShortcuts?: TabShortcutEntry[];
}

function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="font-mono text-2xs px-1.5 py-0.5 rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-text)] shrink-0 whitespace-nowrap">
      {children}
    </kbd>
  );
}

export default function KeyboardHelp({ onClose, tabShortcuts }: KeyboardHelpProps) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="absolute inset-0 bg-[var(--color-bg)] opacity-80" aria-hidden="true" />
      <div className="relative w-full max-w-2xl rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] shadow-[var(--shadow-2)] max-h-[85vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]">
          <h2 className="text-sm font-medium text-[var(--color-text)]">Keyboard shortcuts</h2>
          <button onClick={onClose} className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none" aria-label="Close">×</button>
        </div>

        <div className="px-5 py-4 space-y-4">
          <section>
            <h3 className="text-2xs font-semibold uppercase tracking-widest text-[var(--color-text-faint)] mb-2">
              General
            </h3>
            <ul className="space-y-1.5">
              {BASE_SHORTCUTS.map(({ key, description }) => (
                <li key={key} className="flex items-center gap-3">
                  <span className="min-w-[6.5rem]"><Kbd>{key}</Kbd></span>
                  <span className="text-xs text-[var(--color-text-muted)]">{description}</span>
                </li>
              ))}
            </ul>
          </section>

          {tabShortcuts && tabShortcuts.length > 0 && (
            <section>
              <h3 className="text-2xs font-semibold uppercase tracking-widest text-[var(--color-text-faint)] mb-2">
                Go to view — press <Kbd>g</Kbd> then the key
              </h3>
              <ul className="grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-1.5">
                {tabShortcuts.map(({ key, label }) => (
                  <li key={key} className="flex items-center gap-2">
                    <span className="min-w-[2.75rem]"><Kbd>{key}</Kbd></span>
                    <span className="text-xs text-[var(--color-text-muted)] truncate">{label}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <div className="px-5 py-3 border-t border-[var(--color-border)]">
          <p className="text-2xs text-[var(--color-text-faint)]">
            Press <Kbd>?</Kbd> or <Kbd>Esc</Kbd> to close
          </p>
        </div>
      </div>
    </div>
  );
}
