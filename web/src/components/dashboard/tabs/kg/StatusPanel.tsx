// KG store status panel (ADR-0067). Extracted verbatim from KGTab.tsx
// (dashboard-modernization split).
import { clsx } from 'clsx';
import { labelClass, type KGStatus } from './shared';

function StatusBadge({ status }: { status: KGStatus | null }) {
  if (!status) {
    return (
      <span className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
        loading…
      </span>
    );
  }
  const colorMap: Record<string, string> = {
    ok: 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)]',
    not_initialised: 'text-[var(--color-status-pending)] border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)]',
    error: 'text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)]',
  };
  const cls = colorMap[status.store_health] ?? 'text-[var(--color-text-muted)] border-[var(--color-border)]';
  return (
    <span className={clsx('text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border', cls)}>
      {status.store_health}
    </span>
  );
}

export default function StatusPanel({ status, error }: { status: KGStatus | null; error: string | null }) {
  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">KG Store Status</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            ADR-0067 · KuzuDB-backed Capsule Knowledge Graph
          </p>
        </div>
        <StatusBadge status={status} />
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2 font-mono">
          {error}
        </div>
      )}

      {status && (
        <div className="space-y-2">
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
              <p className={labelClass}>total edges</p>
              <p className="text-lg font-mono font-bold text-[var(--color-text)]">{status.edge_count.toLocaleString()}</p>
            </div>
            <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 col-span-2">
              <p className={labelClass}>db path</p>
              <p className="text-[11px] font-mono text-[var(--color-text-muted)] truncate">{status.db_path}</p>
            </div>
          </div>
          {status.node_counts && Object.keys(status.node_counts).length > 0 && (
            <div className="grid grid-cols-5 gap-2">
              {(['Agent', 'Model', 'MCPServer', 'Tool', 'InferenceEndpoint'] as const).map((label) => (
                <div key={label} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 text-center">
                  <p className="text-[var(--text-2xs)] font-mono uppercase tracking-wider text-[var(--color-text-faint)] truncate">{label === 'InferenceEndpoint' ? 'Endpoint' : label}</p>
                  <p className="text-sm font-mono font-bold text-[var(--color-text)]">{(status.node_counts?.[label] ?? 0).toLocaleString()}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {status?.note && (
        <p className="text-[10px] font-mono text-[var(--color-text-faint)]">{status.note}</p>
      )}
    </section>
  );
}
