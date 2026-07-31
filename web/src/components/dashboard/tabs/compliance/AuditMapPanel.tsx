import { useState, useCallback } from 'react';
import { api } from '../../../../lib/api';

// ---------- Audit Control Map panel (nova audit map --profile) ----------

const AUDIT_MAP_PROFILES = ['nist-ai-rmf', 'eu-ai-act', 'iso-42001', 'nis2'];

export default function AuditMapPanel() {
  const [profile, setProfile] = useState('eu_ai_act');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    profile: string;
    profile_name: string;
    framework: string;
    control_count: number;
    checkers: Array<Record<string, unknown>>;
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      setResult(await api.auditMap(profile));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [profile]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Audit Control Map</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova audit map --profile — list all controls and their evidence checkers for a compliance profile
        </p>
      </div>
      <div className="flex gap-2 items-end">
        <label className="block space-y-1">
          <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Profile</span>
          <select
            value={profile}
            onChange={(e) => setProfile(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          >
            {AUDIT_MAP_PROFILES.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <button
          onClick={run}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 transition-colors"
        >
          {loading ? 'loading…' : 'Show map'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="space-y-2">
          <div className="flex gap-4 text-[10px] font-mono text-[var(--color-text-muted)]">
            <span>{result.profile_name}</span>
            <span>{result.framework}</span>
            <span>{result.control_count} controls</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono border-collapse">
              <thead>
                <tr className="border-b border-[var(--color-border)] text-[var(--color-text-faint)]">
                  <th className="text-left py-1 pr-3">control</th>
                  <th className="text-left py-1 pr-3">title</th>
                  <th className="text-left py-1 pr-3">evidence type</th>
                  <th className="text-left py-1">required</th>
                </tr>
              </thead>
              <tbody>
                {result.checkers.map((c) => (
                  <tr key={String(c.control_id)} className="border-b border-[var(--color-border)] last:border-0">
                    <td className="py-1 pr-3 text-[var(--color-text)]">{String(c.control_id ?? '')}</td>
                    <td className="py-1 pr-3 text-[var(--color-text-muted)]">{String(c.control_title ?? '')}</td>
                    <td className="py-1 pr-3 text-[var(--color-text-faint)]">{String(c.evidence_type ?? '')}</td>
                    <td className="py-1 text-[var(--color-text-faint)]">{c.required ? '✓' : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
