import { test, expect } from '@playwright/test';
import { nextBackoffDelay, openManagedRunStream, RUN_STREAM_BACKOFF } from '../../src/lib/api';

// These tests exercise the managed-reconnect logic (D2b) as pure units — no
// browser required, so they run under the default Playwright Node worker.

test.describe('nextBackoffDelay (D2b)', () => {
  test('starts at the base delay (1s) on the first retry', () => {
    expect(nextBackoffDelay(1)).toBe(RUN_STREAM_BACKOFF.baseMs);
    expect(nextBackoffDelay(1)).toBe(1000);
  });

  test('doubles each consecutive attempt', () => {
    expect(nextBackoffDelay(2)).toBe(2000);
    expect(nextBackoffDelay(3)).toBe(4000);
    expect(nextBackoffDelay(4)).toBe(8000);
  });

  test('caps at maxMs (30s)', () => {
    expect(nextBackoffDelay(99)).toBe(RUN_STREAM_BACKOFF.maxMs);
    expect(nextBackoffDelay(99)).toBe(30000);
  });

  test('clamps non-positive / fractional attempts to the base delay', () => {
    expect(nextBackoffDelay(0)).toBe(1000);
    expect(nextBackoffDelay(-5)).toBe(1000);
    expect(nextBackoffDelay(1.9)).toBe(1000);
  });

  test('honours custom tunables', () => {
    expect(nextBackoffDelay(3, { baseMs: 100, factor: 3, maxMs: 5000 })).toBe(900);
    expect(nextBackoffDelay(10, { baseMs: 100, factor: 3, maxMs: 5000 })).toBe(5000);
  });
});

// Minimal EventSource double letting us drive open/error/message synchronously.
// The managed helper only steps in once readyState === EventSource.CLOSED, so
// the fake reports CLOSED permanently — every emitted error is a hard failure.
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = FakeEventSource.CLOSED;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  closed = false;
  constructor(public url: string) { FakeEventSource.instances.push(this); }
  close(): void { this.closed = true; }
  emitOpen(): void { this.onopen?.(); }
  emitError(): void { this.onerror?.(); }
  emitMessage(data: string): void { this.onmessage?.({ data }); }
}

test.describe('openManagedRunStream reconnect (D2b)', () => {
  test.beforeEach(() => {
    FakeEventSource.instances = [];
    // @ts-expect-error — install the test double over the global.
    globalThis.EventSource = FakeEventSource;
  });

  test('reports connection state and reconnects after a hard failure', async () => {
    const status: boolean[] = [];
    const runs: unknown[] = [];
    const handle = openManagedRunStream((r) => runs.push(r), (connected) => status.push(connected));

    expect(FakeEventSource.instances).toHaveLength(1);
    const es0 = FakeEventSource.instances[0];

    es0.emitOpen();
    expect(status).toContain(true);

    es0.emitMessage(JSON.stringify({ run_id: 'r1' }));
    expect(runs).toHaveLength(1);

    // A hard failure (readyState CLOSED) must NOT permanently kill the stream:
    // the helper tears down the broken EventSource and schedules a reconnect.
    es0.emitError();
    expect(status).toContain(false);
    expect(es0.closed).toBe(true);

    // Backoff resumed: wait past the first delay (1s) and confirm a reconnect.
    await new Promise((res) => setTimeout(res, RUN_STREAM_BACKOFF.baseMs + 100));
    expect(FakeEventSource.instances.length).toBeGreaterThanOrEqual(2);

    handle.close();
    // The active EventSource is torn down on close.
    expect(FakeEventSource.instances.every((es) => es.closed)).toBe(true);
  });

  test('malformed events are ignored without throwing', () => {
    const runs: unknown[] = [];
    const handle = openManagedRunStream((r) => runs.push(r));
    const es = FakeEventSource.instances[0];
    expect(() => es.emitMessage('{not json')).not.toThrow();
    expect(runs).toHaveLength(0);
    handle.close();
  });
});
