/**
 * Sync a piece of UI state to a URL query parameter so tabs are deep-linkable,
 * shareable, and survive back/forward. Mirrors the manual ?run_ids= handling
 * DiffTab already does, generalized for any tab/filter.
 */
import { useCallback, useEffect, useState } from 'react';

function readParam(key: string): string | null {
  if (typeof window === 'undefined') return null;
  return new URLSearchParams(window.location.search).get(key);
}

function writeParam(key: string, value: string | null): void {
  if (typeof window === 'undefined') return;
  const params = new URLSearchParams(window.location.search);
  if (value === null || value === '') params.delete(key);
  else params.set(key, value);
  const qs = params.toString();
  const url = window.location.pathname + (qs ? `?${qs}` : '') + window.location.hash;
  window.history.replaceState({}, '', url);
}

/**
 * String-valued URL state. `defaultValue` is used when the param is absent and
 * the param is omitted from the URL whenever the value equals the default
 * (keeps URLs clean).
 */
export function useUrlState(
  key: string,
  defaultValue = '',
): [string, (next: string) => void] {
  const [value, setValue] = useState<string>(() => readParam(key) ?? defaultValue);

  const set = useCallback((next: string) => {
    setValue(next);
    writeParam(key, next === defaultValue ? null : next);
  }, [key, defaultValue]);

  // Reflect external history navigation (back/forward) into local state.
  useEffect(() => {
    function onPop() { setValue(readParam(key) ?? defaultValue); }
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [key, defaultValue]);

  return [value, set];
}
