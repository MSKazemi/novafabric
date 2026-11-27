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

/** Pure mapping — the state of `extra` given the server's report. */
export function extraState(rows: ExtraRow[] | null | undefined, extra: string): ExtraState {
  if (!rows || rows.length === 0) return { state: 'unknown' };
  const row = rows.find((r) => r.name === extra);
  if (!row || typeof row.installed !== 'boolean') return { state: 'unknown' };
  if (row.installed) return { state: 'available' };
  return {
    state: 'unavailable',
    missing: Array.isArray(row.missing) ? row.missing : [],
    install: row.install || installCommand(extra),
  };
}

let extrasCache: Promise<ExtraRow[] | null> | null = null;

/**
 * The server's extras report, fetched once per page load. A failure resolves
 * to `null` (→ `unknown`) and clears the cache so a later mount retries.
 */
export function loadExtras(): Promise<ExtraRow[] | null> {
  if (!extrasCache) {
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
}

/** React hook: the state of one extra; `unknown` while loading. */
export function useExtraState(extra: string): ExtraState & { loading: boolean } {
  const [rows, setRows] = useState<ExtraRow[] | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    loadExtras().then((r) => {
      if (!live) return;
      setRows(r);
      setLoading(false);
    });
    return () => {
      live = false;
    };
  }, []);

  return { ...extraState(rows, extra), loading };
}
