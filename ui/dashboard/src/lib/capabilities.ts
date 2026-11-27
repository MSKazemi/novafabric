/**
 * Capability-aware feature states (#283, first slice).
 *
 * The dashboard does not invent backend capabilities: it reads the optional
 * extras the server reports in `GET /api/doctor` → `extras` (the data form of
 * `nova doctor --check-extras`) and maps each feature to one of three states:
 *
 *   - `available`   — the server reports the extra fully installed;
 *   - `unavailable` — the server reports it missing, with the exact command
 *                     that installs it;
 *   - `unknown`     — the server did not say (an older backend without the
 *                     field, a source checkout with no package metadata, or
 *                     the doctor call failed). The feature behaves as before;
 *                     the server stays authoritative and answers the action.
 *
 * `unknown` is never rendered as "missing": telling someone to install what
 * they already have is worse than saying nothing.
 */
import { useEffect, useState } from 'react';
import { api } from './api';

/** One row of `/api/doctor` → `extras`. */
export interface ExtraRow {
  name: string;
  installed: boolean;
  requires?: string[];
  missing?: string[];
  install?: string;
}

export type ExtraState =
  | { state: 'available' }
  | { state: 'unavailable'; missing: string[]; install: string }
  | { state: 'unknown' };

/**
 * Dashboard features gated on an optional extra. Every value must be a real
 * `[project.optional-dependencies]` name — `tests/unit/capabilities.test.ts`
 * reads pyproject.toml and fails on a name that does not exist.
 */
export const FEATURE_EXTRAS = {
  sigstore: 'sigstore',
  knowledgeGraph: 'scale-kg',
} as const;

/**
 * The packages of each extra a feature really uses (see `extraState`). The KG
 * store panels need KuzuDB only; the alias table and the entity review queue
 * are SQLite and need nothing from `scale-kg`, so they are never gated.
 */
export const FEATURE_NEEDS = {
  knowledgeGraph: ['kuzu'],
} as const;

/**
 * Words, not colour: what the server says about the framework an adapter
 * wraps. `undefined` is an older server that does not report it.
 */
export function adapterFrameworkLabel(a: { framework_installed?: boolean }): string {
  if (a.framework_installed === true) return 'framework installed';
  if (a.framework_installed === false) return 'framework not installed';
  return 'install state not reported';
}

/** Same quoting as the backend's `install_command` (brackets are shell globs). */
export function installCommand(extra: string): string {
  return `pip install 'novafabric[${extra}]'`;
}

/**
 * Pure mapping — the state of `extra` given the server's report.
 *
 * `needs` narrows the check to the packages a feature really uses. An extra
 * can bundle more than one feature needs (`scale-kg` = `kuzu` + `cachetools`;
 * the KG store needs only `kuzu`), so a feature is unavailable only when one of
 * ITS packages is missing — not whenever the extra as a whole is incomplete.
 */
export function extraState(
  rows: ExtraRow[] | null | undefined,
  extra: string,
  needs?: readonly string[],
): ExtraState {
  if (!rows || rows.length === 0) return { state: 'unknown' };
  const row = rows.find((r) => r.name === extra);
  if (!row || typeof row.installed !== 'boolean') return { state: 'unknown' };
  if (row.installed) return { state: 'available' };
  const missing = Array.isArray(row.missing) ? row.missing : [];
  if (needs && needs.length > 0) {
    const norm = (n: string) => n.toLowerCase().replace(/[-_.]+/g, '-');
    const wanted = new Set(needs.map(norm));
    // Without a `missing` list we cannot tell which package is absent: unknown.
    if (missing.length === 0) return { state: 'unknown' };
    const blocking = missing.filter((m) => wanted.has(norm(m)));
    if (blocking.length === 0) return { state: 'available' };
    return { state: 'unavailable', missing: blocking, install: row.install || installCommand(extra) };
  }
  return { state: 'unavailable', missing, install: row.install || installCommand(extra) };
}

/**
 * How long a fetched report is reused. Installing an extra means restarting
 * `nova serve`; re-reading on window focus after this long picks that up
 * without a manual page reload.
 */
export const EXTRAS_MAX_AGE_MS = 30_000;

let extrasCache: Promise<ExtraRow[] | null> | null = null;
let extrasFetchedAt = 0;

/**
 * The server's extras report, shared by every panel. Reused while younger
 * than `maxAgeMs` (default: until the cache is reset). A failure resolves to
 * `null` (→ `unknown`) and clears the cache so a later call retries.
 */
export function loadExtras(maxAgeMs = Number.POSITIVE_INFINITY): Promise<ExtraRow[] | null> {
  if (extrasCache && Date.now() - extrasFetchedAt > maxAgeMs) extrasCache = null;
  if (!extrasCache) {
    extrasFetchedAt = Date.now();
    // Called inside .then so a synchronous throw (no doctor route on a stub
    // api, a malformed base URL) also lands in .catch and maps to `unknown`.
    extrasCache = Promise.resolve()
      .then(() => api.doctor())
      .then((d) => (d && Array.isArray(d.extras) ? d.extras : null))
      .catch(() => {
        extrasCache = null;
        return null;
      });
  }
  return extrasCache;
}

/** Test seam: forget the cached report. */
export function resetExtrasCache(): void {
  extrasCache = null;
  extrasFetchedAt = 0;
}

/**
 * React hook: the state of one extra (optionally narrowed to `needs`);
 * `unknown` while loading. Re-reads the report when the window regains focus
 * and the cached copy is older than EXTRAS_MAX_AGE_MS, so a server restarted
 * with the extra installed is picked up. A failed re-read keeps the last
 * report rather than flipping a known state to unknown.
 */
export function useExtraState(
  extra: string,
  needs?: readonly string[],
): ExtraState & { loading: boolean } {
  const [rows, setRows] = useState<ExtraRow[] | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    const read = (maxAgeMs?: number) =>
      loadExtras(maxAgeMs).then((r) => {
        if (!live) return;
        setRows((prev) => r ?? prev);
        setLoading(false);
      });
    read();
    const onFocus = () => {
      read(EXTRAS_MAX_AGE_MS);
    };
    window.addEventListener('focus', onFocus);
    return () => {
      live = false;
      window.removeEventListener('focus', onFocus);
    };
  }, []);

  return { ...extraState(rows, extra, needs), loading };
}
