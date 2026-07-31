/**
 * Lazy sparkline eval-history loading via IntersectionObserver: a row's trend
 * cell registers itself and its history is fetched the first time it scrolls
 * into view. Extracted verbatim from the former RegistryTab monolith —
 * behavior frozen.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../../../../lib/api';
import type { EvalHistoryEntry } from '../../EvalSparkline';

export interface SparklineHistoryState {
  sparklineHistory: Record<string, EvalHistoryEntry[]>;
  rowRefCallback: (el: HTMLTableCellElement | null, assetId: string) => void;
  /**
   * Force-refetch one asset's history (used right after an eval run —
   * the IntersectionObserver won't re-fire for an already-visible row).
   */
  refetch: (assetId: string) => void;
}

export function useSparklineHistory(): SparklineHistoryState {
  // Sparkline history cache: assetId → history array. Populated lazily by IntersectionObserver.
  const [sparklineHistory, setSparklineHistory] = useState<Record<string, EvalHistoryEntry[]>>({});
  // Tracks which asset IDs have been fetched. useRef to avoid re-render loops inside observer callback.
  const fetchedIds = useRef<Set<string>>(new Set());
  const observerRef = useRef<IntersectionObserver | null>(null);

  // IntersectionObserver callback ref for sparkline lazy loading
  const rowRefCallback = useCallback((el: HTMLTableCellElement | null, assetId: string) => {
    if (!el) return;
    if (!observerRef.current) {
      observerRef.current = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            const id = (entry.target as HTMLElement).dataset.assetId;
            if (!id || fetchedIds.current.has(id)) continue;
            fetchedIds.current.add(id);
            api.getEvalHistory(id, 10)
              .then((data) => {
                setSparklineHistory((prev) => ({ ...prev, [id]: data.history as EvalHistoryEntry[] }));
              })
              .catch(() => { /* leave empty — sparkline renders null */ });
          }
        },
        { threshold: 0.1 },
      );
    }
    el.dataset.assetId = assetId;
    observerRef.current.observe(el);
  }, []);

  // Disconnect observer on unmount
  useEffect(() => () => { observerRef.current?.disconnect(); }, []);

  const refetch = useCallback((assetId: string) => {
    fetchedIds.current.delete(assetId);
    api.getEvalHistory(assetId, 10)
      .then((data) => {
        setSparklineHistory((prev) => ({ ...prev, [assetId]: data.history as EvalHistoryEntry[] }));
      })
      .catch(() => { /* leave stale on error */ });
  }, []);

  return { sparklineHistory, rowRefCallback, refetch };
}
