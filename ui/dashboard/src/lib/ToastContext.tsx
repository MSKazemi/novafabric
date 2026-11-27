/**
 * App-wide toast notifications.
 *
 * Replaces the ad-hoc `flash`/`onFlash` prop threading that previously only
 * reached RunsTab/RegistryTab. Any component under <ToastProvider> can call
 * `useToast().toast(tone, text)` to surface feedback — the foundation for the
 * unified mutation pattern (`useMutation`) and every action button.
 */
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react';
import { clsx } from 'clsx';

export type ToastTone = 'success' | 'error' | 'info';

export interface ToastItem {
  id: number;
  tone: ToastTone;
  text: string;
}

interface ToastContextValue {
  /** Show a transient toast. Returns the toast id (auto-dismissed after ~4s). */
  toast: (tone: ToastTone, text: string) => number;
  /** Dismiss a toast early by id. */
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const TONE_STYLES: Record<ToastTone, string> = {
  success:
    'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_12%,var(--color-bg-raised))] text-[var(--color-status-success)]',
  error:
    'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_12%,var(--color-bg-raised))] text-[var(--color-status-failure)]',
  info:
    'border-[color-mix(in_oklab,var(--color-accent)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-accent)_12%,var(--color-bg-raised))] text-[var(--color-accent)]',
};

const TONE_GLYPH: Record<ToastTone, string> = { success: '✓', error: '✗', info: 'ℹ' };

/** Spoken prefix: the glyph is aria-hidden and the tone is otherwise colour-only. */
const TONE_LABEL: Record<ToastTone, string> = { success: 'Success:', error: 'Error:', info: 'Info:' };

const TOAST_MS = 4500;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  const hovered = useRef<Set<number>>(new Set());
  const focused = useRef<Set<number>>(new Set());

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    hovered.current.delete(id);
    focused.current.delete(id);
    const handle = timers.current.get(id);
    if (handle) { clearTimeout(handle); timers.current.delete(id); }
  }, []);

  const schedule = useCallback((id: number) => {
    const old = timers.current.get(id);
    if (old) clearTimeout(old);
    timers.current.set(id, setTimeout(() => dismiss(id), TOAST_MS));
  }, [dismiss]);

  // Hovering or focusing a toast holds it on screen (WCAG 2.2.1): a reader who
  // needs longer than 4.5 s is not raced by the auto-dismiss. The timer
  // restarts only once the toast is neither hovered nor focused.
  const hold = useCallback((id: number, why: 'hover' | 'focus') => {
    (why === 'hover' ? hovered : focused).current.add(id);
    const handle = timers.current.get(id);
    if (handle) { clearTimeout(handle); timers.current.delete(id); }
  }, []);
  const release = useCallback((id: number, why: 'hover' | 'focus') => {
    (why === 'hover' ? hovered : focused).current.delete(id);
    if (!hovered.current.has(id) && !focused.current.has(id)) schedule(id);
  }, [schedule]);

  const toast = useCallback((tone: ToastTone, text: string): number => {
    const id = ++seq.current;
    setToasts((prev) => [...prev.slice(-3), { id, tone, text }]);
    schedule(id);
    return id;
  }, [schedule]);

  return (
    <ToastContext.Provider value={{ toast, dismiss }}>
      {children}
      {/* Always mounted, so screen readers announce each toast as it is added. */}
      <div
        aria-live="polite"
        aria-relevant="additions text"
        className="fixed bottom-5 right-5 z-50 flex flex-col gap-2 items-end pointer-events-none"
      >
        {toasts.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => dismiss(t.id)}
            onMouseEnter={() => hold(t.id, 'hover')}
            onMouseLeave={() => release(t.id, 'hover')}
            onFocus={() => hold(t.id, 'focus')}
            onBlur={() => release(t.id, 'focus')}
            title="Dismiss"
            className={clsx(
              'pointer-events-auto rounded-lg border px-4 py-3 shadow-xl max-w-md text-sm font-medium text-left transition-opacity',
              TONE_STYLES[t.tone],
            )}
          >
            <span aria-hidden="true" className="mr-1.5">{TONE_GLYPH[t.tone]}</span>
            <span className="sr-only">{TONE_LABEL[t.tone]} </span>
            {t.text}
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/**
 * Access the toast API. Safe to call outside a provider (no-op fallback) so
 * panels can be rendered in isolation (e.g. Storybook/tests) without crashing.
 */
export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (ctx) return ctx;
  return { toast: () => 0, dismiss: () => {} };
}
