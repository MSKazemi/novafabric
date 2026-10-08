import { useState, useCallback } from 'react';

export function useLocalMru(key: string, max = 12): [string[], (v: string) => void] {
  const [mru, setMru] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem(key) ?? '[]'); } catch { return []; }
  });
  const push = useCallback((v: string) => {
    setMru(prev => {
      const next = [v, ...prev.filter(x => x !== v)].slice(0, max);
      try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* quota */ }
      return next;
    });
  }, [key, max]);
  return [mru, push];
}
