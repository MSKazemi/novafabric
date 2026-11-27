/**
 * Shared guard for the dashboard's single-character keyboard shortcuts
 * (`g <key>` navigation, `r` refresh, `?` help, `j`/`k` in the Runs list).
 *
 * WCAG 2.2 SC 2.1.4 (Character Key Shortcuts) asks that shortcuts made of a
 * single printable key can be turned off: speech-input users and people who
 * type with one switch otherwise trigger them by accident. The preference is a
 * per-browser UI setting (localStorage), on by default — the existing
 * behaviour — and Ctrl/Cmd+K, Escape, Tab and arrow keys are not affected.
 */

export const SINGLE_KEY_SHORTCUTS_KEY = 'novafabric.single-key-shortcuts';

/** True unless the user turned single-key shortcuts off. */
export function singleKeyShortcutsEnabled(): boolean {
  try {
    return localStorage.getItem(SINGLE_KEY_SHORTCUTS_KEY) !== 'off';
  } catch {
    return true;
  }
}

export function setSingleKeyShortcutsEnabled(enabled: boolean): void {
  try {
    if (enabled) localStorage.removeItem(SINGLE_KEY_SHORTCUTS_KEY);
    else localStorage.setItem(SINGLE_KEY_SHORTCUTS_KEY, 'off');
  } catch {
    /* storage unavailable: the setting simply does not persist */
  }
}

/** A field where a printable key is text, not a command. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

/**
 * A modal dialog (confirm, command palette, …) is open. Shortcuts that act on
 * the page underneath must not fire through it. A dialog that opts out with
 * `data-shortcut-passthrough` (the keyboard-help overlay, which `?` closes) is
 * ignored.
 */
export function modalDialogOpen(): boolean {
  if (typeof document === 'undefined') return false;
  return document.querySelector('[aria-modal="true"]:not([data-shortcut-passthrough])') !== null;
}

/**
 * True when a single printable-key shortcut must NOT run for this event:
 * a modifier is held, the user is typing in a field, a modal dialog is open,
 * or the user turned single-key shortcuts off.
 */
export function ignoreSingleKeyShortcut(e: KeyboardEvent): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey) return true;
  if (isTypingTarget(e.target)) return true;
  if (modalDialogOpen()) return true;
  return !singleKeyShortcutsEnabled();
}
