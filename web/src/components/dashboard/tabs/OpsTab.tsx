/**
 * Operations — installation health and runtime control:
 *   nova doctor          → diagnostic checks
 *   nova daemon status   → warm capture daemon liveness (read-only)
 *   nova server flush-jwks-cache → OIDC cache flush
 */
import { useEffect, useState } from 'react';
import { api } from '../../../lib/api';
import { useMutation } from '../../../lib/useMutation';
import TabShell from './TabShell';
import ActionButton from '../../ui/ActionButton';

export default function OpsTab() {
  const [checks, setChecks] = useState<Array<{ name: string; ok: boolean; detail: string }>>([]);
  const [doctorLoading, setDoctorLoading] = useState(true);
  const [daemon, setDaemon] = useState<{ alive: boolean; socket_path?: string } | null>(null);

  const load = () => {
    setDoctorLoading(true);
    api.doctor().then((r) => setChecks(r.checks ?? [])).catch(() => setChecks([])).finally(() => setDoctorLoading(false));
    api.daemonStatus().then((r) => setDaemon({ alive: r.alive, socket_path: r.socket_path })).catch(() => setDaemon(null));
  };
  useEffect(load, []);

  const flushJwks = useMutation(() => api.flushJwksCache(), { successMessage: 'JWKS cache flushed' });

  const okCount = checks.filter((c) => c.ok).length;

  return (
    <TabShell
      title="Operations"
      subtitle="Installation diagnostics, daemon liveness, and runtime cache control."
      cli={['nova doctor', 'nova daemon status', 'nova server flush-jwks-cache']}
      help="Daemon control (start/stop) is intentionally not exposed over HTTP; status is read-only. Process supervision is handled by systemd / Slurm Prolog / a K8s DaemonSet."
      actions={<ActionButton onClick={load} variant="ghost" size="sm">Refresh</ActionButton>}
    >
      {/* Daemon */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Warm capture daemon</h3>
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 flex items-center gap-3">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: daemon?.alive ? 'var(--color-status-success)' : 'var(--color-text-faint)' }} />
          <span className="text-sm text-[var(--color-text)]">{daemon?.alive ? 'Running' : 'Not running'}</span>
          {daemon?.socket_path && <code className="text-[10px] font-mono text-[var(--color-text-faint)] truncate">{daemon.socket_path}</code>}
        </div>
      </section>

      {/* Doctor */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">
          Diagnostics {!doctorLoading && <span className="font-mono text-[var(--color-text-faint)]">{okCount}/{checks.length} ok</span>}
        </h3>
        {doctorLoading ? (
          <p className="text-xs text-[var(--color-text-faint)]">Running checks…</p>
        ) : (
          <div className="rounded border border-[var(--color-border)] divide-y divide-[var(--color-border)]">
            {checks.map((c) => (
              <div key={c.name} className="flex items-center gap-3 px-3 py-2 text-xs">
                <span style={{ color: c.ok ? 'var(--color-status-success)' : 'var(--color-status-failure)' }}>{c.ok ? '✓' : '✗'}</span>
                <span className="font-mono w-48 truncate">{c.name}</span>
                <span className="text-[var(--color-text-muted)] truncate flex-1">{c.detail}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Runtime control */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Runtime</h3>
        <ActionButton
          onClick={() => flushJwks.run()}
          pending={flushJwks.pending}
          confirm={{ title: 'Flush JWKS cache?', body: 'Forces a re-fetch of OIDC signing keys on the next request.' }}
        >
          Flush JWKS cache
        </ActionButton>
      </section>
    </TabShell>
  );
}
