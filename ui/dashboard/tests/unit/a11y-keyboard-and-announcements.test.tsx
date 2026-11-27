/**
 * Keyboard, focus and screen-reader contracts from the #280 accessibility audit.
 *
 * Each block pins one defect that was fixed: shortcuts firing through dialogs
 * or without a way to turn them off (WCAG 2.1.4), focus leaving or jumping
 * inside dialogs (2.4.3), an unannounced command palette and toasts (4.1.3),
 * and trust verdicts carried only by a glyph and colour (1.4.1).
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef, useState } from 'react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import AppearancePanel from '@/components/dashboard/AppearancePanel';
import EvidenceList from '@/components/evidence/EvidenceList';
import CommandPalette, { type Command } from '@/components/ui/CommandPalette';
import DataTable, { type Column } from '@/components/ui/DataTable';
import KeyboardHelp from '@/components/ui/KeyboardHelp';
import Modal from '@/components/ui/primitives/Modal';
import { api, type EvidenceSummary, type VerifyResult } from '@/lib/api';
import {
  ignoreSingleKeyShortcut,
  keyboardHelpOpen,
  modalDialogOpen,
  setSingleKeyShortcutsEnabled,
  singleKeyShortcutsEnabled,
} from '@/lib/shortcuts';
import { ToastProvider, useToast } from '@/lib/ToastContext';

// jsdom implements neither Element.scrollIntoView (the palette keeps the
// highlighted row in view) nor window.matchMedia (AppearancePanel reads the
// colour scheme). Both exist in every browser the dashboard supports, so the
// test environment provides minimal stand-ins rather than the components
// growing jsdom-only guards.
beforeAll(() => {
  if (typeof Element.prototype.scrollIntoView !== 'function') {
    Element.prototype.scrollIntoView = () => {};
  }
  if (typeof window.matchMedia !== 'function') {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function key(k: string, extra: Partial<KeyboardEvent> & { target?: EventTarget | null } = {}): KeyboardEvent {
  return { key: k, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, target: null, ...extra } as unknown as KeyboardEvent;
}

describe('single-key shortcut guard (WCAG 2.1.4)', () => {
  it('lets a plain key through by default', () => {
    expect(singleKeyShortcutsEnabled()).toBe(true);
    expect(ignoreSingleKeyShortcut(key('r'))).toBe(false);
  });

  it('ignores the key with a modifier, inside a field, or once turned off', () => {
    expect(ignoreSingleKeyShortcut(key('k', { ctrlKey: true }))).toBe(true);
    expect(ignoreSingleKeyShortcut(key('k', { metaKey: true }))).toBe(true);
    const input = document.createElement('input');
    expect(ignoreSingleKeyShortcut(key('g', { target: input }))).toBe(true);
    const select = document.createElement('select');
    expect(ignoreSingleKeyShortcut(key('r', { target: select }))).toBe(true);

    setSingleKeyShortcutsEnabled(false);
    expect(singleKeyShortcutsEnabled()).toBe(false);
    expect(ignoreSingleKeyShortcut(key('r'))).toBe(true);
    setSingleKeyShortcutsEnabled(true);
    expect(ignoreSingleKeyShortcut(key('r'))).toBe(false);
  });

  it('ignores the key while any modal dialog is open, the keyboard help included', () => {
    const { unmount } = render(<div role="dialog" aria-modal="true">confirm</div>);
    expect(modalDialogOpen()).toBe(true);
    expect(keyboardHelpOpen()).toBe(false);
    expect(ignoreSingleKeyShortcut(key('g'))).toBe(true);
    unmount();
    // g…/r/j/k must not act on the page behind the help overlay; only `?`
    // (handled by the shell via keyboardHelpOpen) and Escape close it.
    render(<KeyboardHelp onClose={() => {}} />);
    expect(modalDialogOpen()).toBe(true);
    expect(keyboardHelpOpen()).toBe(true);
    expect(ignoreSingleKeyShortcut(key('r'))).toBe(true);
  });

  it('is switchable from the Appearance panel', async () => {
    function Harness() {
      const ref = useRef<HTMLButtonElement>(null);
      return (
        <>
          <button ref={ref}>gear</button>
          <AppearancePanel sidebarCollapsed={false} triggerRef={ref} onClose={() => {}} />
        </>
      );
    }
    render(<Harness />);
    const box = screen.getByRole('checkbox', { name: /Single-key shortcuts/ });
    expect(box).toBeChecked();
    await userEvent.click(box);
    expect(box).not.toBeChecked();
    expect(singleKeyShortcutsEnabled()).toBe(false);
  });
});

describe('focus trap', () => {
  it('Escape closes only the innermost of two stacked dialogs', async () => {
    const outer = vi.fn();
    const inner = vi.fn();
    render(
      <Modal title="Outer" onClose={outer}>
        <button>outer button</button>
        <Modal title="Inner" onClose={inner}><button>inner button</button></Modal>
      </Modal>,
    );
    await userEvent.keyboard('{Escape}');
    expect(inner).toHaveBeenCalledOnce();
    expect(outer).not.toHaveBeenCalled();
  });

  it('a parent re-render with a new inline onClose does not move focus', () => {
    const { rerender } = render(
      <Modal title="T" onClose={() => {}}>
        <button>first</button>
        <button>last</button>
      </Modal>,
    );
    screen.getByText('last').focus();
    rerender(
      <Modal title="T" onClose={() => {}}>
        <button>first</button>
        <button>last</button>
      </Modal>,
    );
    expect(screen.getByText('last')).toHaveFocus();
  });

  it('names the dialog from its title', () => {
    render(<Modal title="Delete asset" onClose={() => {}}><button>ok</button></Modal>);
    expect(screen.getByRole('dialog', { name: 'Delete asset' })).toBeInTheDocument();
  });

  it('keyboard help takes focus and keeps Tab inside', async () => {
    render(<KeyboardHelp onClose={() => {}} />);
    const close = screen.getByRole('button', { name: 'Close' });
    expect(close).toHaveFocus();
    await userEvent.tab();
    expect(close).toHaveFocus();
  });
});

describe('command palette semantics', () => {
  const commands: Command[] = [
    { id: 'a', label: 'Alpha', group: 'View', run: vi.fn() },
    { id: 'b', label: 'Beta', group: 'View', run: vi.fn() },
  ];

  it('is a combobox over a listbox whose active option is announced', async () => {
    render(<CommandPalette commands={commands} onClose={() => {}} />);
    const input = screen.getByRole('combobox', { name: 'Search commands' });
    expect(input).toHaveFocus();
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(2);
    expect(input).toHaveAttribute('aria-activedescendant', options[0]!.id);
    expect(options[0]).toHaveAttribute('aria-selected', 'true');

    await userEvent.keyboard('{ArrowDown}');
    expect(input).toHaveAttribute('aria-activedescendant', options[1]!.id);
    expect(options[1]).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('2 results');
  });

  it('keeps Tab inside the dialog and announces an empty result', async () => {
    render(<CommandPalette commands={commands} onClose={() => {}} />);
    const input = screen.getByRole('combobox', { name: 'Search commands' });
    await userEvent.tab();
    expect(input).toHaveFocus();
    await userEvent.type(input, 'zzz');
    expect(screen.getByRole('status')).toHaveTextContent('No matches.');
    expect(input).not.toHaveAttribute('aria-activedescendant');
  });

  it('returns focus to the opener on Escape', async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>open palette</button>
          {open && <CommandPalette commands={commands} onClose={() => setOpen(false)} />}
        </>
      );
    }
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'open palette' });
    await userEvent.click(opener);
    expect(screen.getByRole('combobox')).toHaveFocus();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener).toHaveFocus();
  });
});

describe('toasts', () => {
  function Trigger() {
    const { toast } = useToast();
    return <button onClick={() => toast('error', 'Boom')}>fire</button>;
  }

  it('are rendered in a polite live region with the tone in words', () => {
    render(<ToastProvider><Trigger /></ToastProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'fire' }));
    const t = screen.getByRole('button', { name: /Error:\s*Boom/ });
    expect(t.closest('[aria-live="polite"]')).not.toBeNull();
  });

  it('stay while hovered and dismiss after the pointer leaves', () => {
    vi.useFakeTimers();
    render(<ToastProvider><Trigger /></ToastProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'fire' }));
    const t = screen.getByRole('button', { name: /Boom/ });
    fireEvent.mouseEnter(t);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(screen.getByRole('button', { name: /Boom/ })).toBeInTheDocument();
    fireEvent.mouseLeave(t);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.queryByRole('button', { name: /Boom/ })).toBeNull();
  });
});

describe('toasts and keyboard focus', () => {
  function Trigger() {
    const { toast } = useToast();
    return <button onClick={() => toast('info', 'Saved')}>fire</button>;
  }

  it('mouse-leave does not restart the timer while the toast has focus', () => {
    vi.useFakeTimers();
    render(<ToastProvider><Trigger /></ToastProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'fire' }));
    const t = screen.getByRole('button', { name: /Saved/ });
    fireEvent.focus(t);
    fireEvent.mouseEnter(t);
    fireEvent.mouseLeave(t);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(screen.getByRole('button', { name: /Saved/ })).toBeInTheDocument();
    fireEvent.blur(t);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.queryByRole('button', { name: /Saved/ })).toBeNull();
  });
});

describe('toast hold while focused', () => {
  function Trigger() {
    const { toast } = useToast();
    return <button onClick={() => toast('info', 'Saved')}>fire</button>;
  }

  it('the pointer leaving does not restart the timer while the toast has focus', () => {
    vi.useFakeTimers();
    render(<ToastProvider><Trigger /></ToastProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'fire' }));
    const t = screen.getByRole('button', { name: /Saved/ });
    fireEvent.mouseEnter(t);
    fireEvent.focus(t);
    fireEvent.mouseLeave(t);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(screen.getByRole('button', { name: /Saved/ })).toBeInTheDocument();
    fireEvent.blur(t);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.queryByRole('button', { name: /Saved/ })).toBeNull();
  });
});

describe('evidence list keyboard path and spoken verdicts', () => {
  const bundles: EvidenceSummary[] = [
    { bundle_id: 'b1', run_id: 'r1', timestamp: '', size_bytes: 10, verified: false },
  ];

  it('selects a bundle from the keyboard', async () => {
    const onSelect = vi.fn();
    render(<EvidenceList bundles={bundles} selectedId={null} onSelect={onSelect} onRefresh={() => {}} />);
    const btn = screen.getByRole('button', { name: 'Show details for run r1' });
    btn.focus();
    await userEvent.keyboard('{Enter}');
    expect(onSelect).toHaveBeenCalledWith('b1');
  });

  it('says the manifest status and each badge result in words', async () => {
    const result: VerifyResult = {
      bundle_id: 'b1',
      run_id: 'r1',
      valid: false,
      signature_ok: true,
      timestamp_ok: null,
      log_integrity_ok: null,
      seal_available: false,
      errors: ['subject mismatch'],
      signer_trust: 'untrusted',
      signer_fingerprints: ['sha256:abc'],
    };
    vi.spyOn(api, 'verifyEvidence').mockResolvedValue(result);
    render(<EvidenceList bundles={bundles} selectedId={null} onSelect={() => {}} onRefresh={() => {}} />);
    expect(screen.getByText('Manifest hash mismatch')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download bundle for run r1' })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() => expect(screen.getByText(/^invalid/)).toBeTruthy());
    expect(screen.getByText(/^invalid/).textContent).toContain('failed');
    expect(screen.getByText(/^sig ✓/).textContent).toContain('passed');
    // Untrusted signer is a failure in words, not only a red glyph.
    expect(screen.getByText(/^signer untrusted/).textContent).toContain('failed');
    expect(screen.getByText(/^tsr –/).textContent).toContain('not established');
  });
});

describe('data table semantics and focus', () => {
  type R = { id: string; name: string };
  const columns: Column<R>[] = [{ key: 'name', header: 'Name' }];
  const rows: R[] = [{ id: 'a', name: 'alpha' }, { id: 'b', name: 'beta' }];

  // jsdom lays nothing out, so the virtualizer sees a 0-px scroller and renders
  // no rows. Give elements a size (restored by the global afterEach).
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800);
  });

  it('exposes table, header and row roles, and clickable rows work from the keyboard', async () => {
    const onRowClick = vi.fn();
    render(<DataTable<R> columns={columns} rows={rows} rowKey={(r) => r.id} onRowClick={onRowClick} />);
    const table = screen.getByRole('table');
    expect(table).toHaveAttribute('aria-rowcount', '3');
    expect(screen.getByRole('columnheader', { name: 'Name' })).toBeInTheDocument();
    const row = screen.getByRole('row', { name: 'alpha' });
    expect(row).toHaveAttribute('tabindex', '0');
    row.focus();
    await userEvent.keyboard('{Enter}');
    expect(onRowClick).toHaveBeenCalledWith(rows[0]);
  });

  it('parks focus on the scroller when the focused row unmounts', () => {
    const { rerender } = render(
      <DataTable<R> columns={columns} rows={rows} rowKey={(r) => r.id} onRowClick={() => {}} />,
    );
    screen.getByRole('row', { name: 'alpha' }).focus();
    rerender(<DataTable<R> columns={columns} rows={[rows[1]!]} rowKey={(r) => r.id} onRowClick={() => {}} />);
    expect(screen.queryByRole('row', { name: 'alpha' })).toBeNull();
    expect(screen.getByRole('rowgroup')).toHaveFocus();
  });
});
