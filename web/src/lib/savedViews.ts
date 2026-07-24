/**
 * savedViews — E2 (ADR-0201): persist named filter presets ("views") for a
 * dashboard list in the browser's localStorage.
 *
 * Deliberately client-only: a saved view is a per-user convenience, not shared
 * server state, so there is no endpoint, no auth surface, and nothing to leak.
 * The store is generic over the filter payload shape and namespaced by a
 * storage key so different tabs keep independent view sets. All reads/writes
 * are defensive — a corrupt or unavailable localStorage degrades to "no saved
 * views", never a thrown error that would break the tab.
 */
import { useCallback, useEffect, useState } from 'react';

export interface SavedView<T> {
  name: string;
  value: T;
}

function storageKeyFor(namespace: string): string {
  return `nova.savedViews.${namespace}`;
}

function readAll<T>(namespace: string): Array<SavedView<T>> {
  try {
    const raw = localStorage.getItem(storageKeyFor(namespace));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (v): v is SavedView<T> => v && typeof v.name === 'string' && 'value' in v,
    );
  } catch {
    return [];
  }
}

function writeAll<T>(namespace: string, views: Array<SavedView<T>>): void {
  try {
    localStorage.setItem(storageKeyFor(namespace), JSON.stringify(views));
  } catch {
    /* storage full or unavailable — saved views are best-effort */
  }
}

/**
 * useSavedViews — manage the named views for one list.
 *
 * `save` upserts by name (re-saving a name overwrites it); `remove` deletes by
 * name. Returns the current views plus the two mutators, all stable-identity.
 */
export function useSavedViews<T>(namespace: string) {
  const [views, setViews] = useState<Array<SavedView<T>>>(() => readAll<T>(namespace));

  // Re-read if the namespace changes (e.g. tab remounts under a new key).
  useEffect(() => {
    setViews(readAll<T>(namespace));
  }, [namespace]);

  const save = useCallback(
    (name: string, value: T) => {
      const trimmed = name.trim();
      if (!trimmed) return;
      setViews((prev) => {
        const next = [...prev.filter((v) => v.name !== trimmed), { name: trimmed, value }];
        next.sort((a, b) => a.name.localeCompare(b.name));
        writeAll(namespace, next);
        return next;
      });
    },
    [namespace],
  );

  const remove = useCallback(
    (name: string) => {
      setViews((prev) => {
        const next = prev.filter((v) => v.name !== name);
        writeAll(namespace, next);
        return next;
      });
    },
    [namespace],
  );

  return { views, save, remove };
}
