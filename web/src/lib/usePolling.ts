import { useEffect, useRef } from 'react';

/**
 * Visibility-aware interval polling.
 *
 * Calls `fn` every `intervalMs`, but PAUSES while the browser tab is hidden
 * (Page Visibility API) and fires `fn` immediately when the tab becomes
 * visible again. This prevents background tabs from hammering the server with
 * full-table queries (a real cost at 100+ concurrent dashboard users).
 *
 * Pass `enabled=false` to stop polling entirely.
 */
export function usePolling(
  fn: () => void,
  intervalMs: number,
  enabled = true,
): void {
  const saved = useRef(fn);
  saved.current = fn;

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return;

    let timer: ReturnType<typeof setInterval> | null = null;

    const tick = () => saved.current();

    const start = () => {
      if (timer != null) return;
      timer = setInterval(tick, intervalMs);
    };
    const stop = () => {
      if (timer != null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const onVisibility = () => {
      if (typeof document !== 'undefined' && document.hidden) {
        stop();
      } else {
        tick(); // refresh immediately on return, then resume
        start();
      }
    };

    if (typeof document !== 'undefined' && !document.hidden) start();
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }

    return () => {
      stop();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
    };
  }, [intervalMs, enabled]);
}
