/**
 * Data layer for the Registry tab: offset/limit asset paging with load-more,
 * plus the per-page detail fetch that feeds the RegistryBrowser adapter and
 * the flattened eval-result list.
 *
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 * Stays a bespoke hook rather than adopting `usePaginatedQuery` because each
 * page fans out into three coupled stores (assets, detailMap, evalResults)
 * and refresh() must also clear the row selection.
 */
import { useState, useEffect, useCallback, useRef, type Dispatch, type SetStateAction } from 'react';
import { api } from '../../../../lib/api';
import type { AssetSummary, AssetDetail } from '../../../../lib/api';
import type { EvalResult } from '../../../../lib/fixtures';

const PAGE_SIZE = 50;

export interface AssetsPageState {
  assets: AssetSummary[] | null;
  detailMap: Map<string, AssetDetail>;
  evalResults: EvalResult[];
  error: string | null;
  totalAssets: number;
  hasMore: boolean;
  loadingMore: boolean;
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
  selectedIds: Set<string>;
  setSelectedIds: Dispatch<SetStateAction<Set<string>>>;
}

export function useAssetsPage({
  refreshTick,
  onCountChange,
}: {
  refreshTick: number;
  onCountChange?: (n: number) => void;
}): AssetsPageState {
  const [assets, setAssets] = useState<AssetSummary[] | null>(null);
  const [detailMap, setDetailMap] = useState<Map<string, AssetDetail>>(new Map());
  const [evalResults, setEvalResults] = useState<EvalResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Pagination state (load-more pattern)
  const [loadedOffset, setLoadedOffset] = useState(0);
  const [totalAssets, setTotalAssets] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const onCountChangeRef = useRef(onCountChange);
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  const _loadPage = useCallback(async (offset: number, replace: boolean) => {
    try {
      const r = await api.listAssets({ limit: PAGE_SIZE, offset });
      if (replace) {
        setAssets(r.assets);
        setSelectedIds(new Set());
      } else {
        setAssets(prev => [...(prev ?? []), ...r.assets]);
      }
      setTotalAssets(r.total);
      setHasMore(r.has_more);
      setLoadedOffset(offset + r.assets.length);
      onCountChangeRef.current?.(r.total);
      // Load details for newly-fetched assets
      const details = await Promise.all(r.assets.map(a => api.getAsset(a.id).catch(() => null)));
      setDetailMap(prev => {
        const next = new Map(prev);
        for (let i = 0; i < r.assets.length; i++) {
          const d = details[i];
          if (d) next.set(r.assets[i].id, d);
        }
        return next;
      });
      setEvalResults(prev => {
        const flat = replace ? [] : [...prev];
        for (let i = 0; i < r.assets.length; i++) {
          const d = details[i];
          if (!d) continue;
          for (const e of d.eval_results ?? []) {
            let score: number | null = null;
            try {
              const parsed = typeof e.score === 'string' ? JSON.parse(e.score) : e.score;
              if (typeof parsed === 'number') score = parsed;
              else if (parsed && typeof parsed === 'object' && 'score' in parsed) {
                const raw = (parsed as { score: unknown }).score;
                score = typeof raw === 'number' ? raw : null;
              }
            } catch { /* ignore */ }
            flat.push({ asset: `${r.assets[i].name}@${r.assets[i].version}`, suite: e.suite_name, passed: e.passed, score });
          }
        }
        return flat;
      });
    } catch (e) {
      setError((e as Error).message);
    }
  }, []); // onCountChange intentionally excluded — accessed via ref

  const refresh = useCallback(async () => {
    setError(null);
    setLoadedOffset(0);
    await _loadPage(0, true);
  }, [_loadPage]);

  const loadMore = useCallback(async () => {
    if (!hasMore || loadingMore) return;
    setLoadingMore(true);
    try {
      await _loadPage(loadedOffset, false);
    } finally {
      setLoadingMore(false);
    }
  }, [hasMore, loadingMore, _loadPage, loadedOffset]);

  useEffect(() => { refresh(); }, [refresh, refreshTick]);

  return {
    assets, detailMap, evalResults, error, totalAssets, hasMore, loadingMore,
    refresh, loadMore, selectedIds, setSelectedIds,
  };
}
