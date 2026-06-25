/**
 * Unified async-mutation hook.
 *
 * Wraps any `api.*` call (or arbitrary async fn) with pending/error/result
 * state and automatic success/error toasts. This is the single pattern every
 * action button in the dashboard uses, so feedback is consistent everywhere.
 *
 * Usage:
 *   const erase = useMutation(api.piiErase, {
 *     successMessage: 'Data subject erased',
 *   });
 *   <ActionButton onClick={() => erase.run(subjectId)} pending={erase.pending}>Erase</ActionButton>
 */
import { useCallback, useRef, useState } from 'react';
import { useToast } from './ToastContext';
import { ServeApiError } from './api';

export interface UseMutationOptions<TResult> {
  /** Toast shown on success. A function receives the result for a dynamic message. */
  successMessage?: string | ((result: TResult) => string);
  /** Toast shown on error. Defaults to the thrown error's message. */
  errorMessage?: string | ((error: Error) => string);
  /** Suppress the automatic success toast (errors still toast unless silenced). */
  silentSuccess?: boolean;
  /** Suppress the automatic error toast. */
  silentError?: boolean;
  /** Called with the result after a successful run (e.g. to refresh a list). */
  onSuccess?: (result: TResult) => void;
  /** Called after a failed run. */
  onError?: (error: Error) => void;
}

export interface MutationState<TArgs extends unknown[], TResult> {
  /** Invoke the mutation. Resolves to the result, or null if it threw. */
  run: (...args: TArgs) => Promise<TResult | null>;
  pending: boolean;
  error: string | null;
  result: TResult | null;
  /** Clear error/result back to the initial state. */
  reset: () => void;
}

function resolveMessage<T>(
  m: string | ((arg: T) => string) | undefined,
  arg: T,
  fallback: string,
): string {
  if (m === undefined) return fallback;
  return typeof m === 'function' ? m(arg) : m;
}

export function useMutation<TArgs extends unknown[], TResult>(
  fn: (...args: TArgs) => Promise<TResult>,
  opts: UseMutationOptions<TResult> = {},
): MutationState<TArgs, TResult> {
  const { toast } = useToast();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TResult | null>(null);
  // Guard against overlapping invocations clobbering state out of order.
  const inflight = useRef(0);

  const run = useCallback(
    async (...args: TArgs): Promise<TResult | null> => {
      const ticket = ++inflight.current;
      setPending(true);
      setError(null);
      try {
        const r = await fn(...args);
        if (ticket !== inflight.current) return r; // superseded — drop state update
        setResult(r);
        if (!opts.silentSuccess) {
          toast('success', resolveMessage(opts.successMessage, r, 'Done'));
        }
        opts.onSuccess?.(r);
        return r;
      } catch (e) {
        const err =
          e instanceof ServeApiError || e instanceof Error
            ? e
            : new Error(String(e));
        if (ticket === inflight.current) setError(err.message);
        if (!opts.silentError) {
          toast('error', resolveMessage(opts.errorMessage, err, err.message));
        }
        opts.onError?.(err);
        return null;
      } finally {
        if (ticket === inflight.current) setPending(false);
      }
    },
    // opts is intentionally spread by value below to avoid stale closures while
    // keeping the callback stable across renders for typical usage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fn, toast, opts.successMessage, opts.errorMessage, opts.silentSuccess, opts.silentError, opts.onSuccess, opts.onError],
  );

  const reset = useCallback(() => {
    setError(null);
    setResult(null);
    setPending(false);
  }, []);

  return { run, pending, error, result, reset };
}
