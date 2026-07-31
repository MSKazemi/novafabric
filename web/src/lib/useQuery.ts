/**
 * Unified async-read hook — the read-side twin of useMutation.
 *
 * Wraps any `api.*` read with loading/error/data state, in-flight ticket
 * guarding (a superseded response never clobbers newer state), and optional
 * integration with the dashboard's global refresh tick.
 *
 * Usage:
 *   const runs = useQuery(() => api.listRuns({ limit: 100 }), [refreshTick]);
 *   if (runs.error) return <ErrorBox message={runs.error} onRetry={runs.reload} />;
 */
import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';

export interface QueryState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /** Re-run the query (keeps current data visible while reloading). */
  reload: () => void;
}

export function useQuery<T>(fn: () => Promise<T>, deps: DependencyList = []): QueryState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const inflight = useRef(0);
  const [tick, setTick] = useState(0);

  // The caller's fn is intentionally captured fresh each run; deps control when.
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    const ticket = ++inflight.current;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fnRef.current().then(
      (result) => {
        if (cancelled || ticket !== inflight.current) return;
        setData(result);
        setLoading(false);
      },
      (e: unknown) => {
        if (cancelled || ticket !== inflight.current) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  return { data, loading, error, reload };
}
