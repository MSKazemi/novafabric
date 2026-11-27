/**
 * Bounded in-memory lists for data fed by a live stream or by repeated paging.
 *
 * A list the dashboard keeps in memory must have a named maximum: a tab left
 * open on a busy server receives new runs for hours, and every "Load more"
 * appends a page. These helpers keep a list newest-first and drop the OLDEST
 * entries once the maximum is reached, so memory stays flat. The UI says when
 * the maximum has been reached ("showing the latest N"); the server stays the
 * authority for anything older (narrow the filter, or use the CLI).
 *
 * Pure functions, no React: unit-tested in tests/unit/bounded-list.test.ts.
 */

/** Most runs the Runs list keeps in memory (stream prepends + loaded pages). */
export const LIVE_RUNS_MAX = 500;

/** Most audit entries the Audit tab keeps in memory (10 pages of 200). */
export const AUDIT_ENTRIES_MAX = 2000;

/** Most assets the Registry tab keeps in memory (20 pages of 50). */
export const REGISTRY_ASSETS_MAX = 1000;

/** Default cap for lists accumulated by `usePaginatedQuery` (no tab uses the hook yet). */
export const PAGINATED_MAX_ITEMS = 2000;

function clampMax(max: number): number {
  return Number.isFinite(max) ? Math.max(1, Math.floor(max)) : 1;
}

/**
 * Put `item` at the head of a newest-first list. An item whose key is already
 * present is not added twice (the list is returned unchanged, same reference).
 * At most `max` items are kept; the oldest (tail) ones are dropped, except the
 * one whose key is `keep` (e.g. the selected row): it stays, as the last entry,
 * and the oldest other entry is dropped instead.
 */
export function prependBounded<T>(
  list: readonly T[] | null | undefined,
  item: T,
  max: number,
  key: (t: T) => string,
  keep?: string | null,
): T[] {
  const cap = clampMax(max);
  const prev = list ?? [];
  const k = key(item);
  if (prev.some((x) => key(x) === k)) return prev as T[];
  const next = [item, ...prev];
  if (next.length <= cap) return next;
  const kept = next.slice(0, cap);
  if (keep == null || cap < 2 || kept.some((x) => key(x) === keep)) return kept;
  const pinned = next.slice(cap).find((x) => key(x) === keep);
  return pinned === undefined ? kept : [...kept.slice(0, cap - 1), pinned];
}

/**
 * Append an older page to a newest-first list, keeping at most `max` items.
 * When the page does not fit, its tail (the oldest items) is dropped and
 * `dropped` says how many.
 */
export function appendBounded<T>(
  list: readonly T[] | null | undefined,
  page: readonly T[],
  max: number,
): { items: T[]; dropped: number } {
  const cap = clampMax(max);
  const prev = list ?? [];
  const room = Math.max(0, cap - prev.length);
  if (page.length <= room) return { items: [...prev, ...page], dropped: 0 };
  return { items: [...prev, ...page.slice(0, room)], dropped: page.length - room };
}

/** True once a list has reached its maximum (no further page may be appended). */
export function isAtCap(list: readonly unknown[] | null | undefined, max: number): boolean {
  return (list?.length ?? 0) >= clampMax(max);
}

/** Split `items` into consecutive batches of at most `size` (a server-side id cap, say). */
export function chunk<T>(items: readonly T[], size: number): T[][] {
  const n = clampMax(size);
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += n) out.push(items.slice(i, i + n));
  return out;
}

/**
 * Keep only the entries of `rec` whose key is in `keep`. Used for per-row side
 * data (e.g. per-run cost) so it cannot outgrow the list it describes.
 */
export function pruneRecord<V>(rec: Record<string, V>, keep: Iterable<string>): Record<string, V> {
  const wanted = new Set(keep);
  const out: Record<string, V> = {};
  for (const [k, v] of Object.entries(rec)) {
    if (wanted.has(k)) out[k] = v;
  }
  return out;
}
