// Shared display helpers for the Seal tab panels. Extracted verbatim from
// SealTab.tsx (dashboard-modernization split).

export function fmt(ts: string | null | undefined): string {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function truncate(s: string, n = 20): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}
