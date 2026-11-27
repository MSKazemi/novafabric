/**
 * Freshness of live data, in words.
 *
 * A list fed by the `/api/runs/stream` SSE stream is only current while the
 * stream is open AND the list has been reloaded since the last gap. The stream
 * has no event IDs and no replay, so runs created while it was down are never
 * delivered: after a reconnect the list must be reloaded ("resynced") before it
 * may be called live again. This module tracks those facts as a small reducer
 * and turns them into the words the UI shows:
 *
 *   live · connecting… · reconnected — refreshing… · reconnecting… ·
 *   offline — showing data from HH:MM · update failed — showing data from HH:MM
 *
 * plus a secondary "last updated 2 min ago". Stale data is never labelled
 * "live". Pure functions, no React: tests/unit/freshness.test.ts.
 */

/** A dropped stream counts as offline after this long without reconnecting. */
export const OFFLINE_AFTER_MS = 30_000;

/** How often the indicator re-renders to age "last updated …" (ms). */
export const FRESHNESS_TICK_MS = 10_000;

export interface LiveTracker {
  /** The stream is open right now. */
  streamOpen: boolean;
  /** The stream has opened at least once. */
  everOpened: boolean;
  /** Epoch ms up to which the shown data is known to be current; null = nothing loaded yet. */
  currentAt: number | null;
  /** Epoch ms when the stream went down; null while it is up (or never opened). */
  downSince: number | null;
  /** A reload after a reconnect is in flight; the list may be missing runs until it lands. */
  resyncing: boolean;
  /** The last reload failed; the list shows older data. */
  syncFailed: boolean;
  /** The browser reports no network (`navigator.onLine === false`). */
  browserOffline: boolean;
}

export type LiveEvent =
  | { type: 'synced'; at: number }
  | { type: 'sync-failed'; at: number }
  | { type: 'resync-started'; at: number }
  | { type: 'stream-open'; at: number; reconnected?: boolean }
  | { type: 'stream-message'; at: number }
  | { type: 'stream-down'; at: number }
  | { type: 'browser-offline'; at: number }
  | { type: 'browser-online'; at: number };

export type LivePhase =
  | 'connecting'
  | 'live'
  | 'resyncing'
  | 'reconnecting'
  | 'offline'
  | 'stale';

export const INITIAL_LIVE_TRACKER: LiveTracker = {
  streamOpen: false,
  everOpened: false,
  currentAt: null,
  downSince: null,
  resyncing: false,
  syncFailed: false,
  browserOffline: false,
};

/** True while the list is both streamed and reloaded since the last gap. */
function isCurrent(s: LiveTracker): boolean {
  return s.streamOpen && !s.resyncing && !s.syncFailed && !s.browserOffline;
}

export function reduceLive(s: LiveTracker, e: LiveEvent): LiveTracker {
  switch (e.type) {
    case 'synced':
      return { ...s, currentAt: e.at, resyncing: false, syncFailed: false };
    case 'sync-failed':
      return { ...s, resyncing: false, syncFailed: true };
    case 'resync-started':
      return { ...s, resyncing: true };
    case 'stream-open':
      // An open after a gap may have missed runs: the list is not current until resynced.
      return {
        ...s,
        streamOpen: true,
        everOpened: true,
        downSince: null,
        resyncing: e.reconnected ? true : s.resyncing,
      };
    case 'stream-message':
      // A pushed run only keeps the list current if nothing was missed before it.
      return isCurrent(s) ? { ...s, currentAt: e.at } : s;
    case 'stream-down':
      if (!s.streamOpen && s.downSince !== null) return s;
      return {
        ...s,
        // The data was current right up to the moment the stream dropped.
        currentAt: isCurrent(s) ? e.at : s.currentAt,
        streamOpen: false,
        downSince: s.downSince ?? e.at,
      };
    case 'browser-offline':
      return {
        ...s,
        currentAt: isCurrent(s) ? e.at : s.currentAt,
        browserOffline: true,
        downSince: s.downSince ?? e.at,
      };
    case 'browser-online':
      return { ...s, browserOffline: false, downSince: s.streamOpen ? null : s.downSince };
    default:
      return s;
  }
}

export function livePhase(s: LiveTracker, now: number): LivePhase {
  if (s.browserOffline) return 'offline';
  if (s.streamOpen) {
    if (s.resyncing) return 'resyncing';
    if (s.syncFailed) return 'stale';
    return 'live';
  }
  if (s.downSince !== null) {
    return now - s.downSince >= OFFLINE_AFTER_MS ? 'offline' : 'reconnecting';
  }
  return 'connecting';
}

/** "less than a minute ago", "2 min ago", "3 h ago", "2 d ago". */
export function agoText(thenMs: number, now: number): string {
  const diff = Math.max(0, now - thenMs);
  const min = Math.floor(diff / 60_000);
  if (min < 1) return 'less than a minute ago';
  if (min < 60) return `${min} min ago`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.floor(h / 24)} d ago`;
}

/** Local wall-clock "HH:MM". */
export function clockText(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export type FreshnessTone = 'ok' | 'pending' | 'bad';

export interface FreshnessText {
  /** Short state, announced politely to screen readers when it changes. */
  label: string;
  /** Secondary, not announced: "last updated 2 min ago". */
  detail: string | null;
  tone: FreshnessTone;
  /** Longer explanation for the tooltip. */
  title: string;
}

export function describeFreshness(
  phase: LivePhase,
  currentAt: number | null,
  now: number,
  clock: (ms: number) => string = clockText,
): FreshnessText {
  const updated = currentAt !== null ? `last updated ${agoText(currentAt, now)}` : null;
  const from = currentAt !== null ? `showing data from ${clock(currentAt)}` : 'no data loaded yet';
  switch (phase) {
    case 'live':
      return {
        label: 'live',
        detail: null,
        tone: 'ok',
        title: 'Live stream connected — new runs appear automatically.',
      };
    case 'resyncing':
      return {
        label: 'reconnected — refreshing…',
        detail: updated,
        tone: 'pending',
        title: 'The live stream is back. The list is reloading because runs created while it was down are not replayed.',
      };
    case 'reconnecting':
      return {
        label: 'reconnecting…',
        detail: updated,
        tone: 'pending',
        title: 'The live stream dropped. Retrying with backoff; the list is not updating meanwhile.',
      };
    case 'offline':
      return {
        label: `offline — ${from}`,
        detail: updated,
        tone: 'bad',
        title: 'The server is unreachable. The list shows the data loaded before the connection was lost and is not current.',
      };
    case 'stale':
      return {
        label: `update failed — ${from}`,
        detail: updated,
        tone: 'bad',
        title: 'Reloading the list failed. Use refresh to try again; the list is not current.',
      };
    case 'connecting':
    default:
      return {
        label: 'connecting…',
        detail: updated,
        tone: 'pending',
        title: 'Opening the live stream. Until it connects, the list is as of the last load.',
      };
  }
}
