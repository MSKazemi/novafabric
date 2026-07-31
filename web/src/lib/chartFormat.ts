/**
 * Shared axis/tick formatting for the zero-dependency SVG charts (ADR-0201:
 * no charting library — so the formatting niceties live here instead).
 */

/** 1234 → "1.2K", 5_600_000 → "5.6M". Keeps small numbers exact. */
export function formatSI(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${trimZero(value / 1e9)}B`;
  if (abs >= 1e6) return `${trimZero(value / 1e6)}M`;
  if (abs >= 1e3) return `${trimZero(value / 1e3)}K`;
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(abs < 10 ? 1 : 0);
}

function trimZero(v: number): string {
  const s = v.toFixed(1);
  return s.endsWith('.0') ? s.slice(0, -2) : s;
}

/** Milliseconds → human duration ("340ms", "2.5s", "4m 10s"). */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s % 60);
  if (m < 60) return rest ? `${m}m ${rest}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** Evenly spaced "nice" ticks for a [0, max] axis. */
export function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0];
  const step = niceStep(max / count);
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

function niceStep(rough: number): number {
  const pow = 10 ** Math.floor(Math.log10(rough));
  const frac = rough / pow;
  const nice = frac >= 5 ? 10 : frac >= 2 ? 5 : frac >= 1 ? 2 : 1;
  return nice * pow;
}

/** "2026-07-30" / ISO timestamp → short day label ("Jul 30"). */
export function formatDay(iso: string): string {
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}
