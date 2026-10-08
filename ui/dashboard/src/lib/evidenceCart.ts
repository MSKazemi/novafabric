/**
 * Evidence cart — ADR-0239 D1/D2/D7 (experimental).
 *
 * A session-scoped, ordered list of **references** collected while
 * investigating. It holds no copies (D2: resolved once, at export, server-side)
 * and lives in `sessionStorage` (D7: ephemeral and local — gone when the
 * browser session ends, never persisted server-side).
 *
 * Every item records `added_at` and `added_from` (the view URL it was added
 * from) because that is chain-of-custody information: who added what, when,
 * and from which view.
 *
 * All storage access is defensive — an unavailable or corrupt sessionStorage
 * degrades to an empty cart, never a thrown error that breaks the tab.
 */
import { useCallback, useEffect, useState } from 'react';

export type CartKind =
  | 'run' | 'capsule' | 'lineage_query' | 'diff' | 'chart' | 'policy_decision' | 'audit_record';

export interface CartEntry {
  kind: CartKind;
  ref: string;
  added_at: string;
  added_from?: string;
  note?: string;
}

const STORAGE_KEY = 'nova.evidenceCart.v1';
const CHANGE_EVENT = 'nova:evidence-cart';

/** Mirrors the server bound (serve/routers/evidence_cart.py MAX_CART_ITEMS). */
export const MAX_CART_ITEMS = 50;

function readCart(): CartEntry[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (e): e is CartEntry =>
        !!e && typeof e === 'object'
        && typeof (e as CartEntry).kind === 'string'
        && typeof (e as CartEntry).ref === 'string'
        && typeof (e as CartEntry).added_at === 'string',
    );
  } catch {
    return [];
  }
}

function writeCart(items: CartEntry[]): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(items));
  } catch {
    /* storage unavailable — the in-memory cart still works for this view */
  }
  try {
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT));
  } catch { /* non-browser */ }
}

/** The current view, as a relative URL — what `added_from` records. */
export function currentViewRef(): string {
  if (typeof window === 'undefined') return '';
  return `${window.location.pathname}${window.location.search}`;
}

/** Pure: add a reference, idempotent on (kind, ref), order preserved. */
export function addEntry(items: CartEntry[], entry: CartEntry): CartEntry[] {
  if (items.some(e => e.kind === entry.kind && e.ref === entry.ref)) return items;
  return [...items, entry];
}

export function removeEntry(items: CartEntry[], kind: CartKind, ref: string): CartEntry[] {
  return items.filter(e => !(e.kind === kind && e.ref === ref));
}

export function useEvidenceCart() {
  const [items, setItems] = useState<CartEntry[]>(() => readCart());

  useEffect(() => {
    const sync = () => setItems(readCart());
    window.addEventListener(CHANGE_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(CHANGE_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  const add = useCallback((kind: CartKind, ref: string, note?: string) => {
    const next = addEntry(readCart(), {
      kind, ref, added_at: new Date().toISOString(), added_from: currentViewRef(),
      ...(note ? { note } : {}),
    });
    writeCart(next);
    setItems(next);
  }, []);

  const remove = useCallback((kind: CartKind, ref: string) => {
    const next = removeEntry(readCart(), kind, ref);
    writeCart(next);
    setItems(next);
  }, []);

  const clear = useCallback(() => {
    writeCart([]);
    setItems([]);
  }, []);

  const has = useCallback(
    (kind: CartKind, ref: string) => items.some(e => e.kind === kind && e.ref === ref),
    [items],
  );

  return { items, add, remove, clear, has };
}
