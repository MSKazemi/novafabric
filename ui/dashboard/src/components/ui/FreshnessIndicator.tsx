/**
 * Says, in words, how current a live list is: "live", "reconnecting…",
 * "offline — showing data from 14:05", plus "last updated 2 min ago".
 *
 * The state word sits in a polite live region (`role="status"`), so a screen
 * reader hears it when it changes. The ageing "last updated …" text is outside
 * that region: it changes every tick and must not be re-announced. The colour
 * dot is decorative; the words carry the meaning.
 */
import { useEffect, useState } from 'react';
import { clsx } from 'clsx';
import {
  FRESHNESS_TICK_MS,
  describeFreshness,
  livePhase,
  type FreshnessTone,
  type LiveTracker,
} from '../../lib/freshness';

export interface FreshnessIndicatorProps {
  tracker: LiveTracker;
  /** What the words describe, for the accessible name (e.g. "Runs list"). */
  subject?: string;
  /** Re-render interval for the ageing text (ms). */
  tickMs?: number;
  /** Clock formatter for "showing data from HH:MM" (tests inject a fixed one). */
  clock?: (ms: number) => string;
}

const DOT: Record<FreshnessTone, string> = {
  ok: 'bg-[var(--color-status-success)] animate-pulse',
  pending: 'bg-[var(--color-status-pending)]',
  bad: 'bg-[var(--color-status-failure)]',
};

const TEXT: Record<FreshnessTone, string> = {
  ok: 'text-[var(--color-status-success)]',
  pending: 'text-[var(--color-text-muted)]',
  bad: 'text-[var(--color-text)]',
};

export default function FreshnessIndicator({
  tracker,
  subject = 'Data',
  tickMs = FRESHNESS_TICK_MS,
  clock,
}: FreshnessIndicatorProps) {
  // The tick only forces a re-render; the time itself is read at render.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (tickMs <= 0) return;
    const id = setInterval(() => setTick((t) => t + 1), tickMs);
    return () => clearInterval(id);
  }, [tickMs]);

  const now = Date.now();
  const phase = livePhase(tracker, now);
  const f = describeFreshness(phase, tracker.currentAt, now, clock);

  return (
    <span
      className="flex items-center gap-1 text-2xs font-mono"
      data-freshness={phase}
      title={f.title}
    >
      <span aria-hidden="true" className={clsx('w-1.5 h-1.5 rounded-full inline-block', DOT[f.tone])} />
      <span
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className={TEXT[f.tone]}
      >
        <span className="sr-only">{subject}: </span>
        {f.label}
      </span>
      {f.detail && (
        <span className="text-[var(--color-text-faint)]">· {f.detail}</span>
      )}
    </span>
  );
}
