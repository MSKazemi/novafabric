/**
 * Issue #282 — the managed run stream reconnects with capped, jittered
 * exponential backoff, never stampedes, and tells its caller when an open
 * follows a gap (the stream has no replay, so the list must be reloaded).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  RUN_STREAM_BACKOFF,
  jitteredBackoffDelay,
  nextBackoffDelay,
  openManagedRunStream,
  type RunStreamStatus,
} from '@/lib/api';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  // CONNECTING on purpose: the wrapper must take over even while the browser
  // would keep retrying on its own fixed interval.
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  closed = false;
  constructor(public url: string) { FakeEventSource.instances.push(this); }
  close(): void { this.closed = true; this.readyState = FakeEventSource.CLOSED; }
  open(): void { this.readyState = FakeEventSource.OPEN; this.onopen?.(); }
  fail(): void { this.onerror?.(); }
  message(data: string): void { this.onmessage?.({ data }); }
}

const last = () => FakeEventSource.instances[FakeEventSource.instances.length - 1];
const openCount = () => FakeEventSource.instances.filter((es) => !es.closed).length;

function setOnline(value: boolean) {
  // jsdom's window.navigator and the global `navigator` may be distinct objects.
  for (const nav of new Set([window.navigator, globalThis.navigator])) {
    if (nav) Object.defineProperty(nav, 'onLine', { configurable: true, get: () => value });
  }
}

describe('jitteredBackoffDelay', () => {
  it('stays within [d/2, d] of the capped exponential delay', () => {
    for (const attempt of [1, 2, 3, 4, 5, 6, 10, 50]) {
      const d = nextBackoffDelay(attempt);
      expect(jitteredBackoffDelay(attempt, () => 0)).toBe(d / 2);
      expect(jitteredBackoffDelay(attempt, () => 1)).toBe(d);
      const mid = jitteredBackoffDelay(attempt, () => 0.5);
      expect(mid).toBeGreaterThanOrEqual(d / 2);
      expect(mid).toBeLessThanOrEqual(d);
    }
  });

  it('never exceeds the cap and never drops below half the base', () => {
    expect(jitteredBackoffDelay(99, () => 1)).toBe(RUN_STREAM_BACKOFF.maxMs);
    expect(jitteredBackoffDelay(1, () => 0)).toBe(RUN_STREAM_BACKOFF.baseMs / 2);
  });

  it('treats a broken random source as 0 instead of NaN', () => {
    expect(jitteredBackoffDelay(2, () => Number.NaN)).toBe(nextBackoffDelay(2) / 2);
    expect(jitteredBackoffDelay(2, () => 7)).toBe(nextBackoffDelay(2));
    expect(jitteredBackoffDelay(2, () => -3)).toBe(nextBackoffDelay(2) / 2);
  });
});

describe('openManagedRunStream', () => {
  const realEventSource = globalThis.EventSource;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
    setOnline(true);
  });

  afterEach(() => {
    vi.useRealTimers();
    globalThis.EventSource = realEventSource;
    setOnline(true);
  });

  it('takes over on any error and backs off exponentially, capped', () => {
    const statuses: RunStreamStatus[] = [];
    // random() = 1 → the upper end of each jitter window, i.e. the plain delay.
    const h = openManagedRunStream(() => {}, (_c, s) => statuses.push(s), { random: () => 1 });

    const delays: number[] = [];
    for (let i = 0; i < 8; i++) {
      last().fail();
      expect(openCount()).toBe(0); // the failed source is closed, nothing reconnects yet
      const s = statuses[statuses.length - 1];
      expect(s.connected).toBe(false);
      delays.push(s.retryInMs as number);
      vi.advanceTimersByTime(s.retryInMs as number);
      expect(openCount()).toBe(1);
    }
    expect(delays).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    h.close();
  });

  it('opens at most one connection at a time — no reconnect storm', () => {
    const h = openManagedRunStream(() => {}, undefined, { random: () => 0.5 });
    for (let i = 0; i < 20; i++) {
      last().fail();
      last().fail(); // a late duplicate error on the dead source is ignored
      vi.advanceTimersByTime(RUN_STREAM_BACKOFF.maxMs);
      expect(openCount()).toBeLessThanOrEqual(1);
    }
    // 1 initial + 20 reconnects; the duplicate errors scheduled nothing extra.
    expect(FakeEventSource.instances).toHaveLength(21);
    h.close();
  });

  it('does not reset the backoff for a connection that opens and drops at once', () => {
    const statuses: RunStreamStatus[] = [];
    const h = openManagedRunStream(() => {}, (_c, s) => statuses.push(s), { random: () => 1 });
    const retries: number[] = [];
    for (let i = 0; i < 4; i++) {
      last().open();
      last().fail(); // flapping: accepted, then dropped immediately
      retries.push(statuses[statuses.length - 1].retryInMs as number);
      vi.advanceTimersByTime(retries[retries.length - 1]);
    }
    expect(retries).toEqual([1000, 2000, 4000, 8000]);
    h.close();
  });

  it('resets the backoff after a stable connection', () => {
    const statuses: RunStreamStatus[] = [];
    const h = openManagedRunStream(() => {}, (_c, s) => statuses.push(s), { random: () => 1 });
    last().fail();
    vi.advanceTimersByTime(1000);
    last().fail();
    vi.advanceTimersByTime(2000);
    last().open();
    vi.advanceTimersByTime(RUN_STREAM_BACKOFF.stableMs);
    last().fail();
    expect(statuses[statuses.length - 1].retryInMs).toBe(1000);
    h.close();
  });

  it('flags the first open after a failure as reconnected (resync needed)', () => {
    const statuses: RunStreamStatus[] = [];
    const h = openManagedRunStream(() => {}, (_c, s) => statuses.push(s), { random: () => 1 });
    last().open();
    expect(statuses[statuses.length - 1]).toMatchObject({ connected: true, reconnected: false });
    last().fail();
    vi.advanceTimersByTime(1000);
    last().open();
    expect(statuses[statuses.length - 1]).toMatchObject({ connected: true, reconnected: true });
    h.close();
  });

  it('waits for the browser to come back online instead of retrying blind', () => {
    const statuses: RunStreamStatus[] = [];
    const h = openManagedRunStream(() => {}, (_c, s) => statuses.push(s), { random: () => 1 });
    setOnline(false);
    last().fail();
    expect(statuses[statuses.length - 1]).toMatchObject({ connected: false, waitingForNetwork: true, retryInMs: null });
    vi.advanceTimersByTime(10 * RUN_STREAM_BACKOFF.maxMs);
    expect(FakeEventSource.instances).toHaveLength(1); // no attempt while offline

    setOnline(true);
    window.dispatchEvent(new Event('online'));
    window.dispatchEvent(new Event('online')); // a repeated event schedules nothing extra
    vi.advanceTimersByTime(RUN_STREAM_BACKOFF.baseMs);
    expect(FakeEventSource.instances).toHaveLength(2);
    h.close();
  });

  it('close() cancels a pending retry and ignores later events', () => {
    const runs: unknown[] = [];
    const h = openManagedRunStream((r) => runs.push(r), undefined, { random: () => 1 });
    const first = last();
    first.fail();
    h.close();
    vi.advanceTimersByTime(10 * RUN_STREAM_BACKOFF.maxMs);
    expect(FakeEventSource.instances).toHaveLength(1);
    first.message(JSON.stringify({ run_id: 'late' }));
    expect(runs).toHaveLength(0);
    window.dispatchEvent(new Event('online'));
    vi.advanceTimersByTime(RUN_STREAM_BACKOFF.maxMs);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
