/**
 * Issue #282 — live data says how current it is, in words, and stale data is
 * never labelled "live".
 */
import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  INITIAL_LIVE_TRACKER,
  OFFLINE_AFTER_MS,
  agoText,
  describeFreshness,
  livePhase,
  reduceLive,
  type LiveEvent,
  type LiveTracker,
} from '@/lib/freshness';
import FreshnessIndicator from '@/components/ui/FreshnessIndicator';

const T0 = 1_700_000_000_000;
const run = (events: LiveEvent[], from: LiveTracker = INITIAL_LIVE_TRACKER) => events.reduce(reduceLive, from);
const clock = (ms: number) => `@${ms - T0}`;

describe('reduceLive + livePhase', () => {
  it('starts as connecting', () => {
    expect(livePhase(INITIAL_LIVE_TRACKER, T0)).toBe('connecting');
  });

  it('is live once the stream is open and the list loaded', () => {
    const s = run([{ type: 'synced', at: T0 }, { type: 'stream-open', at: T0 }]);
    expect(livePhase(s, T0)).toBe('live');
  });

  it('a pushed run keeps a live list current', () => {
    const s = run([
      { type: 'synced', at: T0 },
      { type: 'stream-open', at: T0 },
      { type: 'stream-message', at: T0 + 5000 },
    ]);
    expect(s.currentAt).toBe(T0 + 5000);
  });

  it('a dropped stream is "reconnecting" and keeps the moment data was last current', () => {
    const s = run([
      { type: 'synced', at: T0 },
      { type: 'stream-open', at: T0 },
      { type: 'stream-down', at: T0 + 60_000 },
    ]);
    expect(livePhase(s, T0 + 60_000)).toBe('reconnecting');
    expect(s.currentAt).toBe(T0 + 60_000);
  });

  it('turns offline after OFFLINE_AFTER_MS without reconnecting', () => {
    const s = run([
      { type: 'synced', at: T0 },
      { type: 'stream-open', at: T0 },
      { type: 'stream-down', at: T0 },
    ]);
    expect(livePhase(s, T0 + OFFLINE_AFTER_MS - 1)).toBe('reconnecting');
    expect(livePhase(s, T0 + OFFLINE_AFTER_MS)).toBe('offline');
  });

  it('repeated stream-down events keep the first drop time', () => {
    const s = run([
      { type: 'stream-open', at: T0 },
      { type: 'stream-down', at: T0 },
      { type: 'stream-down', at: T0 + 20_000 },
    ]);
    expect(s.downSince).toBe(T0);
  });

  it('a reopened stream is NOT live until the list is reloaded', () => {
    const s = run([
      { type: 'synced', at: T0 },
      { type: 'stream-open', at: T0 },
      { type: 'stream-down', at: T0 + 1000 },
      { type: 'stream-open', at: T0 + 40_000, reconnected: true },
    ]);
    expect(livePhase(s, T0 + 40_000)).toBe('resyncing');
    // A run pushed before the reload lands does not make old data current.
    const pushed = reduceLive(s, { type: 'stream-message', at: T0 + 41_000 });
    expect(pushed.currentAt).toBe(T0 + 1000);
    const synced = reduceLive(pushed, { type: 'synced', at: T0 + 42_000 });
    expect(livePhase(synced, T0 + 42_000)).toBe('live');
    expect(synced.currentAt).toBe(T0 + 42_000);
  });

  it('a failed reload with the stream open is stale, not live', () => {
    const s = run([
      { type: 'synced', at: T0 },
      { type: 'stream-open', at: T0 },
      { type: 'sync-failed', at: T0 + 30_000 },
    ]);
    expect(livePhase(s, T0 + 30_000)).toBe('stale');
    expect(reduceLive(s, { type: 'stream-message', at: T0 + 31_000 }).currentAt).toBe(T0);
  });

  it('the browser going offline wins over an open stream', () => {
    const s = run([
      { type: 'synced', at: T0 },
      { type: 'stream-open', at: T0 },
      { type: 'browser-offline', at: T0 + 10_000 },
    ]);
    expect(livePhase(s, T0 + 10_000)).toBe('offline');
    expect(s.currentAt).toBe(T0 + 10_000);
    expect(livePhase(reduceLive(s, { type: 'browser-online', at: T0 + 20_000 }), T0 + 20_000)).toBe('live');
  });
});

describe('agoText', () => {
  it('words', () => {
    expect(agoText(T0, T0 + 10_000)).toBe('less than a minute ago');
    expect(agoText(T0, T0 + 2 * 60_000)).toBe('2 min ago');
    expect(agoText(T0, T0 + 3 * 3_600_000)).toBe('3 h ago');
    expect(agoText(T0, T0 + 2 * 86_400_000)).toBe('2 d ago');
    expect(agoText(T0 + 5000, T0)).toBe('less than a minute ago'); // clock skew clamps
  });
});

describe('describeFreshness', () => {
  it('uses the agreed words', () => {
    const now = T0 + 2 * 60_000;
    expect(describeFreshness('live', T0, now, clock).label).toBe('live');
    expect(describeFreshness('reconnecting', T0, now, clock)).toMatchObject({
      label: 'reconnecting…',
      detail: 'last updated 2 min ago',
    });
    expect(describeFreshness('offline', T0, now, clock).label).toBe('offline — showing data from @0');
    expect(describeFreshness('offline', null, now, clock).label).toBe('offline — no data loaded yet');
    expect(describeFreshness('resyncing', T0, now, clock).label).toBe('reconnected — refreshing…');
    expect(describeFreshness('stale', T0, now, clock).label).toBe('update failed — showing data from @0');
    expect(describeFreshness('connecting', null, now, clock)).toMatchObject({ label: 'connecting…', detail: null });
  });

  it('only "live" has the ok tone', () => {
    for (const phase of ['connecting', 'resyncing', 'reconnecting', 'offline', 'stale'] as const) {
      expect(describeFreshness(phase, T0, T0, clock).tone).not.toBe('ok');
      expect(describeFreshness(phase, T0, T0, clock).label).not.toBe('live');
    }
  });
});

describe('FreshnessIndicator', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
  });
  afterEach(() => vi.useRealTimers());

  it('puts the state word in a polite live region', () => {
    const live = run([{ type: 'synced', at: T0 }, { type: 'stream-open', at: T0 }]);
    render(<FreshnessIndicator tracker={live} subject="Runs list" clock={clock} />);
    const region = screen.getByRole('status');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toHaveTextContent('Runs list: live');
  });

  it('ages into "offline — showing data from …" on its own tick, outside the live region', () => {
    const down = run([
      { type: 'synced', at: T0 },
      { type: 'stream-open', at: T0 },
      { type: 'stream-down', at: T0 },
    ]);
    const { container } = render(<FreshnessIndicator tracker={down} clock={clock} tickMs={5000} />);
    expect(screen.getByRole('status')).toHaveTextContent('reconnecting…');
    act(() => { vi.advanceTimersByTime(OFFLINE_AFTER_MS + 5000); });
    expect(screen.getByRole('status')).toHaveTextContent('offline — showing data from @0');
    // The ageing detail is shown, but not inside the announced region.
    expect(container).toHaveTextContent('last updated less than a minute ago');
    expect(screen.getByRole('status')).not.toHaveTextContent('last updated');
    act(() => { vi.advanceTimersByTime(2 * 60_000); });
    expect(container).toHaveTextContent('last updated 2 min ago');
    expect(container.querySelector('[data-freshness]')).toHaveAttribute('data-freshness', 'offline');
  });
});
