/**
 * Sync a piece of UI state to a URL query parameter so tabs are deep-linkable,
 * shareable, and survive back/forward. Mirrors the manual ?run_ids= handling
 * DiffTab already does, generalized for any tab/filter.
 *
 * ADR-0232 D2 — "Browser Back is a first-class undo": pass `{ push: true }` for
 * deliberate, committed view changes (applying a filter, changing scope) so
 * each one is a history entry; the default `replaceState` suits high-frequency
 * changes (keyboard selection, typing) that would otherwise flood history.
 */
import { useCallback, useEffect, useState } from 'react';

function readParam(key: string): string | null {
  if (typeof window === 'undefined') return null;
  return new URLSearchParams(window.location.search).get(key);
}

function writeParam(key: string, value: string | null, push: boolean): void {
  if (typeof window === 'undefined') return;
  const params = new URLSearchParams(window.location.search);
  if (value === null || value === '') params.delete(key);
  else params.set(key, value);
  const qs = params.toString();
  const url = window.location.pathname + (qs ? `?${qs}` : '') + window.location.hash;
  if (url === window.location.pathname + window.location.search + window.location.hash) return;
  if (push) window.history.pushState({}, '', url);
  else window.history.replaceState({}, '', url);
}

export interface UrlStateOptions {
  /** Record the change as a new history entry (Back undoes it). */
  push?: boolean;
}

/**
 * String-valued URL state. `defaultValue` is used when the param is absent and
 * the param is omitted from the URL whenever the value equals the default
 * (keeps URLs clean).
 */
export function useUrlState(
  key: string,
  defaultValue = '',
  options: UrlStateOptions = {},
): [string, (next: string) => void] {
  const [value, setValue] = useState<string>(() => readParam(key) ?? defaultValue);
  const push = options.push === true;

  const set = useCallback((next: string) => {
    setValue(next);
    writeParam(key, next === defaultValue ? null : next, push);
  }, [key, defaultValue, push]);

  // Reflect external history navigation (back/forward) into local state.
  useEffect(() => {
    function onPop() { setValue(readParam(key) ?? defaultValue); }
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [key, defaultValue]);

  return [value, set];
}
