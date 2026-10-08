/**
 * Data layer for the filter bar: when a filter is applied, the run list comes
 * from `GET /api/filter/runs` (ADR-0232 D1 + ADR-0233) instead of the cursor
 * search. Inactive (empty filter) → no request, `result` null.
 */
import { useEffect, useState } from 'react';
import { api, type FilterRunsResult } from '../../../../lib/api';
import type { FilterScope } from './viewState';

/** Inclusive end-of-day for a `YYYY-MM-DD` date input. */
export function endOfDay(date: string): string | undefined {
  return date ? `${date}T23:59:59Z` : undefined;
}

export function useFilteredRuns({
  filter,
  scope,
  since,
  until,
  refreshTick,
  fetcher = api.filterRuns,
}: {
  filter: string;
  scope: FilterScope;
  since: string;
  until: string;
  refreshTick: number;
  fetcher?: typeof api.filterRuns;
}): { result: FilterRunsResult | null; loading: boolean; error: string | null } {
  const [result, setResult] = useState<FilterRunsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!filter) { setResult(null); setError(null); setLoading(false); return; }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetcher({
      f: filter,
      scope,
      since: since || undefined,
      until: endOfDay(until),
      limit: 200,
    })
      .then(r => { if (!cancelled) setResult(r); })
      .catch(e => { if (!cancelled) { setResult(null); setError((e as Error).message); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [filter, scope, since, until, refreshTick, fetcher]);

  return { result, loading, error };
}
