/**
 * Pins useMutation's state machine before PanelScaffold builds on it.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';
import { ToastProvider } from '@/lib/ToastContext';
import { useMutation } from '@/lib/useMutation';

const wrapper = ({ children }: { children: ReactNode }) => (
  <ToastProvider>{children}</ToastProvider>
);

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('useMutation', () => {
  it('idle → pending → result', async () => {
    const d = deferred<string>();
    const { result } = renderHook(
      () => useMutation(() => d.promise, { silentSuccess: true }),
      { wrapper },
    );
    expect(result.current.pending).toBe(false);

    let run: Promise<string | null>;
    act(() => {
      run = result.current.run();
    });
    expect(result.current.pending).toBe(true);

    await act(async () => {
      d.resolve('ok');
      await run;
    });
    expect(result.current.pending).toBe(false);
    expect(result.current.result).toBe('ok');
    expect(result.current.error).toBeNull();
  });

  it('failure sets error and returns null', async () => {
    const { result } = renderHook(
      () =>
        useMutation(async () => {
          throw new Error('boom');
        }, { silentError: true }),
      { wrapper },
    );
    let out: unknown;
    await act(async () => {
      out = await result.current.run();
    });
    expect(out).toBeNull();
    expect(result.current.error).toBe('boom');
    expect(result.current.pending).toBe(false);
  });

  it('a superseding run drops the stale result', async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    let call = 0;
    const { result } = renderHook(
      () =>
        useMutation(
          () => (++call === 1 ? first.promise : second.promise),
          { silentSuccess: true },
        ),
      { wrapper },
    );
    act(() => {
      void result.current.run();
      void result.current.run();
    });
    await act(async () => {
      second.resolve('second');
      first.resolve('first'); // resolves late — must not clobber
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.result).toBe('second'));
  });

  it('reset clears error and result', async () => {
    const { result } = renderHook(
      () => useMutation(async () => 'x', { silentSuccess: true }),
      { wrapper },
    );
    await act(async () => {
      await result.current.run();
    });
    expect(result.current.result).toBe('x');
    act(() => result.current.reset());
    expect(result.current.result).toBeNull();
    expect(result.current.error).toBeNull();
  });
});
