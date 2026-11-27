/**
 * React wrapper around the `freshness` reducer: holds a `LiveTracker` and
 * follows the browser's online/offline events. The data hook that owns a live
 * list dispatches the stream and reload events; `FreshnessIndicator` renders
 * the result (and owns the clock tick, so ageing "last updated …" re-renders
 * only the indicator, never the whole list).
 */
import { useEffect, useReducer, type Dispatch } from 'react';
import { INITIAL_LIVE_TRACKER, reduceLive, type LiveEvent, type LiveTracker } from './freshness';

function initialTracker(): LiveTracker {
  const offline = typeof navigator !== 'undefined' && navigator.onLine === false;
  return { ...INITIAL_LIVE_TRACKER, browserOffline: offline };
}

export function useLiveTracker(): [LiveTracker, Dispatch<LiveEvent>] {
  const [tracker, dispatch] = useReducer(reduceLive, undefined, initialTracker);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onOffline = () => dispatch({ type: 'browser-offline', at: Date.now() });
    const onOnline = () => dispatch({ type: 'browser-online', at: Date.now() });
    window.addEventListener('offline', onOffline);
    window.addEventListener('online', onOnline);
    return () => {
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('online', onOnline);
    };
  }, []);

  return [tracker, dispatch];
}
