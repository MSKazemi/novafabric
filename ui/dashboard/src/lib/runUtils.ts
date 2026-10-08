import type { RunSummary } from './api';

export function runLabel(r: RunSummary): string {
  const pyArg = [...r.command].reverse().find(a => a.endsWith('.py')) ?? '';
  const parts = pyArg.split('/');
  const dir = parts.length >= 2 ? parts[parts.length - 2] : pyArg.replace('.py', '');
  const extra = [...r.command].reverse().find(a => /\.(err|csv|log|json)$/.test(a)) ?? '';
  const shortExtra = extra.split('/').pop()?.replace(/\.\w+$/, '') ?? '';
  return shortExtra ? `${dir}  ${shortExtra}` : (dir || r.run_id.slice(0, 10));
}

export function shortTs(ts: string | null): string {
  if (!ts) return '';
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts.slice(11, 16) : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
