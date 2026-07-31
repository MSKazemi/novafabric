// Multi-layer topology summary panel. Extracted verbatim from KGTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { api } from '../../../../lib/api';
import { labelClass, type KGTopology } from './shared';

const LAYER_COLORS: Record<string, string> = {
  Agent: 'text-[var(--color-accent)]',
  Model: 'text-[var(--color-status-success)]',
  MCPServer: 'text-[var(--color-status-pending)]',
  Tool: 'text-[var(--color-text)]',
  InferenceEndpoint: 'text-[var(--color-text-muted)]',
};

const EDGE_LABELS: Record<string, string> = {
  CALLS: 'Agent → Model',
  USES_TOOL: 'Agent → Tool',
  ROUTES_TO: 'Agent → Endpoint',
  SERVED_BY: 'Tool → MCP Server',
};

export default function TopologyLayerPanel() {
  const [topo, setTopo] = useState<KGTopology | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.kgTopology();
      setTopo(r);
      setLoaded(true);
      if (!r.ok && r.error) setError(r.error);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  const totalNodes = topo ? Object.values(topo.node_counts).reduce((a, b) => a + b, 0) : 0;
  const totalEdges = topo ? Object.values(topo.edge_counts).reduce((a, b) => a + b, 0) : 0;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Multi-Layer Topology</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Auto-discovered from capsules · auto-ingest interval set by <code className="font-mono">NOVA_KG_INGEST_INTERVAL</code> (default 60 s)
          </p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] hover:text-[var(--color-text)] transition-colors"
        >
          {loading ? '…' : loaded ? 'Refresh' : 'Load topology'}
        </button>
      </div>

      {!loaded && !loading && (
        <p className="text-[10px] text-[var(--color-text-faint)] italic">
          Click "Load topology" to query the KG graph.
        </p>
      )}

      {error && (
        <div className="text-[10px] font-mono text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {topo?.ok && topo.truncated && (
        <div className="text-[10px] font-mono text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-pending)_25%,transparent)] rounded px-3 py-2">
          Graph bounded for rendering — {(topo.truncated_reason ?? []).join('; ') || 'result capped'}.
          Showing {topo.nodes.length.toLocaleString()} of {totalNodes.toLocaleString()} nodes.
        </div>
      )}

      {topo?.ok && (
        <div className="space-y-2">
          <div className="grid grid-cols-5 gap-2">
            {(['Agent', 'Model', 'MCPServer', 'Tool', 'InferenceEndpoint'] as const).map((label) => (
              <div key={label} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-2 text-center">
                <p className="text-[var(--text-2xs)] font-mono uppercase tracking-wider text-[var(--color-text-faint)] truncate">
                  {label === 'InferenceEndpoint' ? 'Endpoint' : label}
                </p>
                <p className={`text-base font-mono font-bold ${LAYER_COLORS[label] ?? ''}`}>
                  {(topo.node_counts[label] ?? 0).toLocaleString()}
                </p>
              </div>
            ))}
          </div>

          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-2 space-y-1">
            <p className={labelClass}>edge breakdown</p>
            <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
              {Object.entries(EDGE_LABELS).map(([rel, label]) => (
                <div key={rel} className="flex items-center justify-between text-[10px] font-mono">
                  <span className="text-[var(--color-text-faint)]">{label}</span>
                  <span className="text-[var(--color-text-muted)]">{(topo.edge_counts[rel] ?? 0).toLocaleString()}</span>
                </div>
              ))}
            </div>
          </div>

          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            {totalNodes.toLocaleString()} nodes · {totalEdges.toLocaleString()} edges across 4 relationship types
          </p>
        </div>
      )}

      {topo && !topo.ok && (
        <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
          {topo.note ?? 'KG not initialised — use Init Schema below.'}
        </p>
      )}
    </section>
  );
}
