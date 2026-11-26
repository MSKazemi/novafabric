// System diagnostics (nova doctor). Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { Panel, SectionHeading } from './helpers';
import SupportHandoff from '../../SupportHandoff';

export default function DoctorPanel() {
  const [checks, setChecks] = useState<Array<{ name: string; ok: boolean; detail: string }> | null>(null);
  const [loading, setLoading] = useState(false);
  const [doctorOk, setDoctorOk] = useState<boolean | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setErr(null);
    // Clear previous results so a stale failure is not shown as the new check runs.
    setChecks(null);
    setDoctorOk(null);
    try {
      const r = await api.doctor();
      setDoctorOk(r.ok ?? null);
      setChecks(r.checks);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <Panel>
      <div className="flex items-center justify-between mb-3">
        <SectionHeading>System Diagnostics</SectionHeading>
        <button
          onClick={run}
          disabled={loading}
          className="text-[10px] font-mono px-2.5 py-1 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
        >
          {loading ? 'running…' : 'nova doctor'}
        </button>
      </div>
      <p className="text-xs text-[var(--color-text-muted)] mb-3">
        Check all NovaFabric subsystems: capsule dir, registry DB, lineage store, OPA binary, NovaSeal, KG store, and Python version.
      </p>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {doctorOk === false && (
        <p role="alert" className="text-xs text-[var(--color-status-failure)]">
          The overall diagnostic check reported a failure, even if no individual check is listed as failed.
        </p>
      )}
      {checks !== null && (
        <div className="rounded border border-[var(--color-border)] overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)]">
              <tr>
                <th className="text-left px-3 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Check</th>
                <th className="text-left px-3 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Status</th>
                <th className="text-left px-3 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {checks.map((c) => (
                <tr key={c.name} className="hover:bg-[var(--color-bg-sunken)] transition-colors">
                  <td className="px-3 py-2 font-mono text-[var(--color-text)] text-[10px]">{c.name}</td>
                  <td className="px-3 py-2">
                    <span className={clsx(
                      'text-2xs font-mono uppercase tracking-wider px-1.5 py-0.5 rounded',
                      c.ok
                        ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                        : 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]',
                    )}>
                      {c.ok ? 'ok' : 'fail'}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-[var(--color-text-muted)] text-[10px] font-mono break-all">{c.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(err !== null || doctorOk === false || checks?.some((c) => !c.ok) === true) && <SupportHandoff context="health" />}
    </Panel>
  );
}
