import { useCallback, useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import { SuggestInput } from '../../ui/SuggestInput';
import CopyButton from '../../ui/CopyButton';
import EmptyState from '../../ui/EmptyState';

// DB-KG-1 — Capsule Knowledge Graph tab (v0.18.0)
// Backend: GET /api/kg/status, GET /api/kg/agents/{id}/edges
// Reference: design/adr/0067-capsule-knowledge-graph-v1.md

interface KGStatus {
  store: string;
  store_health: string;
  db_path: string;
  edge_count: number;
  node_counts?: Record<string, number>;
  note?: string;
}

interface KGTopology {
  ok: boolean;
  node_counts: Record<string, number>;
  edge_counts: Record<string, number>;
  nodes: Array<{ id: string; type: string; name?: string; provider?: string; url?: string }>;
  edges: Array<{ src: string; src_type: string; dst: string; dst_type: string; edge_type: string; call_count: number; confidence: number }>;
  note?: string;
  error?: string;
}

interface KGEdge {
  call_count: number;
  verified_count: number;
  confidence: number;
}

interface ModelEdge extends KGEdge { model_id: string }
interface ToolEdge extends KGEdge { tool_id: string }
interface MCPServerEdge { server_id: string; server_name: string; call_count: number }

const inputClass =
  'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

const labelClass =
  'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

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

function StatusPanel({ status, error }: { status: KGStatus | null; error: string | null }) {
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
                  <p className="text-[9px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] truncate">{label === 'InferenceEndpoint' ? 'Endpoint' : label}</p>
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

function EdgeTable<T extends KGEdge>({
  title,
  rows,
  refKey,
  emptyMsg,
}: {
  title: string;
  rows: T[];
  refKey: keyof T;
  emptyMsg: string;
}) {
  if (!rows.length) {
    return (
      <div className="text-[10px] font-mono text-[var(--color-text-faint)] px-3 py-2">
        {emptyMsg}
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px] font-mono">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
            <th className="text-left px-3 py-1.5">{title}</th>
            <th className="text-right px-3 py-1.5">calls</th>
            <th className="text-right px-3 py-1.5">verified</th>
            <th className="text-right px-3 py-1.5">confidence</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-[var(--color-border)] last:border-b-0">
              <td className="px-3 py-1.5 text-[var(--color-text)]">{String(row[refKey])}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{row.call_count.toLocaleString()}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{row.verified_count.toLocaleString()}</td>
              <td className="px-3 py-1.5 text-right text-[var(--color-text-muted)]">{row.confidence.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AgentQueryPanel({ runIds }: { runIds: string[] }) {
  const [agentId, setAgentId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelEdge[]>([]);
  const [tools, setTools] = useState<ToolEdge[]>([]);
  const [mcpServers, setMcpServers] = useState<MCPServerEdge[]>([]);

  const run = useCallback(async () => {
    const id = agentId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.kgAgentEdges(id);
      setModels(res.models);
      setTools(res.tools);
      setMcpServers(res.mcp_servers ?? []);
    } catch (e) {
      setError((e as Error).message);
      setModels([]);
      setTools([]);
      setMcpServers([]);
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  const cliCmd = `nova kg query ${agentId.trim() || '<agent-id>'} --output text`;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Query Agent</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Models, tools, and MCP servers reachable from this agent (CRDT-aggregated)
          </p>
        </div>
      </div>

      <div className="space-y-1">
        <label className={labelClass}>Agent ID</label>
        <SuggestInput
          value={agentId}
          onChange={setAgentId}
          suggestions={runIds}
          placeholder="agent-001 or @capsule run-id"
          className={inputClass + ' w-full'}
          onEnter={run}
        />
      </div>

      <button
        onClick={run}
        disabled={loading || !agentId.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !agentId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'querying…' : 'Query'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2 font-mono">
          {error}
        </div>
      )}

      {(models.length > 0 || tools.length > 0 || mcpServers.length > 0 || !loading) && agentId.trim() && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] divide-y divide-[var(--color-border)]">
          <EdgeTable<ModelEdge>
            title="model_id"
            rows={models}
            refKey="model_id"
            emptyMsg="No models recorded for this agent."
          />
          <EdgeTable<ToolEdge>
            title="tool_id"
            rows={tools}
            refKey="tool_id"
            emptyMsg="No tools recorded for this agent."
          />
          {/* MCP Servers — 2-hop: Agent → Tool → MCPServer */}
          <div className="px-3 py-2">
            <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] mb-1">
              mcp_server (via tool)
            </p>
            {mcpServers.length === 0 ? (
              <p className="text-[10px] text-[var(--color-text-faint)] italic">
                No MCP servers recorded for this agent.
              </p>
            ) : (
              <table className="w-full text-[10px] font-mono">
                <thead>
                  <tr className="text-[var(--color-text-faint)]">
                    <th className="text-left py-0.5">server_id</th>
                    <th className="text-right py-0.5">calls</th>
                  </tr>
                </thead>
                <tbody>
                  {mcpServers.map((s) => (
                    <tr key={s.server_id} className="border-t border-[var(--color-border)]">
                      <td className="py-0.5 text-[var(--color-accent)]">{s.server_id}</td>
                      <td className="py-0.5 text-right text-[var(--color-text-muted)]">{s.call_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      <div className="space-y-1">
        <p className={labelClass}>CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[10px] font-mono text-[var(--color-text-muted)]">{cliCmd}</pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
      </div>
    </section>
  );
}

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

function TopologyLayerPanel() {
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

      {topo?.ok && (
        <div className="space-y-2">
          <div className="grid grid-cols-5 gap-2">
            {(['Agent', 'Model', 'MCPServer', 'Tool', 'InferenceEndpoint'] as const).map((label) => (
              <div key={label} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-2 text-center">
                <p className="text-[9px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] truncate">
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

export default function KGTab() {
  const [status, setStatus] = useState<KGStatus | null>(null);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  const [runIds, setRunIds] = useState<string[]>([]);

  useEffect(() => {
    api.kgStatus()
      .then(setStatus)
      .catch((e) => setStatusErr((e as Error).message));
    api.listRuns()
      .then((r) => setRunIds(r.runs.map((run) => run.run_id)))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-[var(--color-text)]">Capsule Knowledge Graph</h2>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Auto-discovered topology: agents, models, MCP servers, tools, endpoints — CRDT-aggregated across capsules (ADR-0067)
          </p>
        </div>
        <div className="flex gap-1.5">
          {(['ADR-0067', 'v0.27.0'] as const).map((label) => (
            <span
              key={label}
              className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      <StatusPanel status={status} error={statusErr} />
      <TopologyLayerPanel />
      <AgentQueryPanel runIds={runIds} />
      <KGInitPanel onDone={() => {
        api.kgStatus().then(setStatus).catch(() => {});
      }} />
      <KGIngestPanel capsuleDirs={runIds} />
      <KGIngestAllPanel onDone={() => {
        api.kgStatus().then(setStatus).catch(() => {});
      }} />
      <KGQueryPanel />
      <KGAuditPanel />
      <EntityQueuePanel />
      <KGAliasPanel />

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-2">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          About the Capsule KG
        </p>
        <p className="text-xs text-[var(--color-text-muted)]">
          The KG is a <span className="font-semibold">secondary derived artifact</span> built from
          observed capsule events. It is stored in a SEPARATE KuzuDB instance from lineage and
          accumulates CRDT counts so distributed collectors can merge without coordination.
        </p>
        <ul className="text-xs text-[var(--color-text-muted)] space-y-1 pl-3 list-disc">
          <li>Optional extra: <code className="font-mono">pip install 'novafabric[scale-kg]'</code> (kuzu&gt;=0.11.3)</li>
        </ul>
      </div>
    </div>
  );
}

function KGInitPanel({ onDone }: { onDone: () => void }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; db_path?: string; note?: string; error?: string } | null>(null);

  const handleInit = useCallback(async () => {
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgInit();
      setResult(r);
      if (r.ok) onDone();
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [onDone]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Initialise KG Schema</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            nova kg init — idempotent DDL setup (safe to re-run)
          </p>
        </div>
        <button
          onClick={handleInit}
          disabled={loading}
          className={clsx(
            'text-xs font-mono px-3 py-1.5 rounded border transition-colors shrink-0',
            loading
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'initialising…' : 'Init schema'}
        </button>
      </div>

      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)] text-[var(--color-status-success)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] text-[var(--color-status-failure)]',
        )}>
          {result.ok ? `✓ ${result.note}` : `✗ ${result.error}`}
          {result.db_path && <div className="text-[10px] text-[var(--color-text-faint)] mt-1">{result.db_path}</div>}
        </div>
      )}
    </section>
  );
}

function KGIngestPanel({ capsuleDirs }: { capsuleDirs: string[] }) {
  const [capsulePath, setCapsulePath] = useState('');
  const [verified, setVerified] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; ingested?: number; written?: number; skipped?: number; error?: string } | null>(null);

  const inputClass = 'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const handleIngest = useCallback(async () => {
    if (!capsulePath.trim()) return;
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgIngest(capsulePath.trim(), verified);
      setResult(r);
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [capsulePath, verified]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Ingest Capsule into KG</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg ingest — reads model-calls.jsonl + tool-calls.jsonl from a capsule directory
        </p>
      </div>

      <div className="space-y-2">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Capsule path</label>
          <SuggestInput
            value={capsulePath}
            onChange={setCapsulePath}
            suggestions={capsuleDirs}
            placeholder="/data/nova/capsules/01KRK8H9... or run_id"
            className={inputClass}
            onEnter={handleIngest}
          />
        </div>
        <label className="flex items-center gap-2 text-xs text-[var(--color-text-muted)] cursor-pointer">
          <input type="checkbox" checked={verified} onChange={(e) => setVerified(e.target.checked)} className="rounded" />
          Mark events as NovaSeal-verified (confidence = 1.0)
        </label>
      </div>

      <button
        onClick={handleIngest}
        disabled={loading || !capsulePath.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !capsulePath.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'ingesting…' : 'Ingest'}
      </button>

      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)]',
        )}>
          {result.ok
            ? <span className="text-[var(--color-status-success)]">✓ Ingested {result.ingested} events → wrote {result.written} KG edges{result.skipped ? ` (${result.skipped} skipped)` : ''}</span>
            : <span className="text-[var(--color-status-failure)]">✗ {result.error}</span>
          }
        </div>
      )}
    </section>
  );
}

// ── KGIngestAllPanel ─────────────────────────────────────────────────────────
function KGIngestAllPanel({ onDone }: { onDone: () => void }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    total?: number;
    newly_ingested?: number;
    skipped?: number;
    failed?: number;
    error?: string;
  } | null>(null);

  const handleIngestAll = useCallback(async () => {
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgIngestAll();
      setResult(r);
      if (r.ok) onDone();
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [onDone]);

  const cliCmd = 'nova kg ingest --all';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Re-ingest All Capsules</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            nova kg ingest --all — scans the server&apos;s capsule directory and ingests any capsules not yet in the KG
          </p>
        </div>
        <button
          onClick={handleIngestAll}
          disabled={loading}
          className={clsx(
            'text-xs font-mono px-3 py-1.5 rounded border transition-colors shrink-0',
            loading
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {loading ? 'ingesting…' : 'Re-ingest All'}
        </button>
      </div>

      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono space-y-0.5',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)]',
        )}>
          {result.ok ? (
            <>
              <div className="text-[var(--color-status-success)]">
                ✓ {result.total} capsule(s) scanned · {result.newly_ingested} newly ingested
              </div>
              <div className="text-[var(--color-text-faint)] text-[10px]">
                skipped (already tracked): {result.skipped ?? 0} · failed: {result.failed ?? 0}
              </div>
            </>
          ) : (
            <span className="text-[var(--color-status-failure)]">✗ {result.error}</span>
          )}
        </div>
      )}

      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[10px] font-mono text-[var(--color-text-muted)]">{cliCmd}</pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
      </div>
    </section>
  );
}

// ── KGQueryPanel ──────────────────────────────────────────────────────────────
function KGQueryPanel() {
  const [agentId, setAgentId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean; agent_id?: string; models?: string[]; tools?: string[]; error?: string;
  } | null>(null);

  const handleQuery = useCallback(async () => {
    if (!agentId.trim()) return;
    setLoading(true);
    setResult(null);
    try {
      const r = await api.kgQuery(agentId.trim());
      setResult(r);
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">KG Agent Query</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg query &lt;agent_id&gt; — list models and tools observed for an agent
        </p>
      </div>
      <div className="space-y-1">
        <label className={labelClass}>Agent ID</label>
        <input
          type="text"
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleQuery()}
          placeholder="agent-001 or run_id"
          className={`w-full ${inputClass}`}
        />
      </div>
      <button
        onClick={handleQuery}
        disabled={loading || !agentId.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !agentId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'querying…' : 'Query'}
      </button>
      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono space-y-1',
          result.ok
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)]',
        )}>
          {result.ok ? (
            <>
              <div className="text-[var(--color-status-success)]">Agent: {result.agent_id}</div>
              <div>Models: {result.models?.join(', ') || '(none)'}</div>
              <div>Tools: {result.tools?.join(', ') || '(none)'}</div>
            </>
          ) : (
            <span className="text-[var(--color-status-failure)]">✗ {result.error}</span>
          )}
        </div>
      )}
    </section>
  );
}

// ── KGAuditPanel ──────────────────────────────────────────────────────────────
function KGAuditPanel() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    store_health?: string;
    node_count?: number;
    edge_count?: number;
    orphaned_edges?: number;
    zero_call_count?: number;
    issues?: string[];
    error?: string;
  } | null>(null);

  const handleAudit = useCallback(async () => {
    setLoading(true);
    setResult(null);
    try {
      setResult(await api.kgAudit());
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">KG Audit</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg audit — health check: orphaned edges, zero-call nodes, store integrity
        </p>
      </div>
      <button
        onClick={handleAudit}
        disabled={loading}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'auditing…' : 'Run Audit'}
      </button>
      {result && (
        <div className={clsx(
          'rounded px-3 py-2 text-xs font-mono space-y-1',
          result.ok && (!result.issues || result.issues.length === 0)
            ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)]'
            : 'bg-[color-mix(in_oklab,var(--color-status-warning,var(--color-status-pending))_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-pending)_25%,transparent)]',
        )}>
          {result.ok ? (
            <>
              <div>Health: <span className="text-[var(--color-status-success)]">{result.store_health}</span></div>
              <div>Nodes: {result.node_count ?? '—'} | Edges: {result.edge_count ?? '—'}</div>
              <div>Orphaned: {result.orphaned_edges ?? 0} | Zero-call: {result.zero_call_count ?? 0}</div>
              {result.issues && result.issues.length > 0
                ? <div className="text-[var(--color-status-pending)]">Issues: {result.issues.join('; ')}</div>
                : <div className="text-[var(--color-status-success)]">✓ No issues found</div>
              }
            </>
          ) : (
            <span className="text-[var(--color-status-failure)]">✗ {result.error ?? result.store_health}</span>
          )}
        </div>
      )}
    </section>
  );
}

// ── EntityQueuePanel ──────────────────────────────────────────────────────────
function EntityQueuePanel() {
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState<{ ok: boolean; pending: number; approved: number; rejected: number } | null>(null);
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]);
  const [approveInputs, setApproveInputs] = useState<Record<string, string>>({});
  const [actionResult, setActionResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setActionResult(null);
    try {
      const [statsR, listR] = await Promise.all([api.kgEntityQueueStats(), api.kgEntityQueueList()]);
      setStats(statsR);
      setItems(listR.items);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleApprove = useCallback(async (itemId: string) => {
    const canonical = approveInputs[itemId]?.trim();
    if (!canonical) return;
    try {
      await api.kgEntityQueueApprove(itemId, canonical);
      setActionResult(`Approved ${itemId}`);
      load();
    } catch (e) {
      setActionResult(`Error: ${(e as Error).message}`);
    }
  }, [approveInputs, load]);

  const handleReject = useCallback(async (itemId: string) => {
    try {
      await api.kgEntityQueueReject(itemId);
      setActionResult(`Rejected ${itemId}`);
      load();
    } catch (e) {
      setActionResult(`Error: ${(e as Error).message}`);
    }
  }, [load]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Entity Review Queue</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            nova kg entity-queue list/approve/reject/stats — Tier-3 human review for ambiguous aliases
          </p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] transition-colors"
        >
          {loading ? '…' : 'Refresh'}
        </button>
      </div>
      {stats && (
        <div className="flex gap-3 text-[10px] font-mono">
          <span className="text-[var(--color-status-pending)]">pending {stats.pending}</span>
          <span className="text-[var(--color-status-success)]">approved {stats.approved}</span>
          <span className="text-[var(--color-status-failure)]">rejected {stats.rejected}</span>
        </div>
      )}
      {actionResult && (
        <p className="text-[10px] font-mono text-[var(--color-text-muted)]">{actionResult}</p>
      )}
      {!stats && !loading && (
        <p className="text-xs text-[var(--color-text-faint)]">Click Refresh to load queue.</p>
      )}
      {items.length > 0 && (
        <div className="space-y-2 max-h-64 overflow-y-auto">
          {items.map((item) => {
            const id = String(item.item_id ?? '');
            return (
              <div key={id} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-xs space-y-1">
                <div className="font-mono text-[var(--color-text-muted)]">{id}</div>
                <div>Alias: <span className="font-mono">{String(item.alias ?? '')}</span> | Type: {String(item.entity_type ?? '')}</div>
                <div className="text-[10px] text-[var(--color-text-faint)]">Suggested: {String(item.suggested_canonical ?? '—')}</div>
                <div className="flex gap-2 items-center mt-1">
                  <input
                    type="text"
                    placeholder="canonical to approve"
                    value={approveInputs[id] ?? ''}
                    onChange={(e) => setApproveInputs((prev) => ({ ...prev, [id]: e.target.value }))}
                    className={`flex-1 ${inputClass}`}
                  />
                  <button
                    onClick={() => handleApprove(id)}
                    className="text-[10px] font-mono px-2 py-1 rounded border border-[color-mix(in_oklab,var(--color-status-success)_50%,transparent)] text-[var(--color-status-success)] hover:bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] transition-colors"
                  >Approve</button>
                  <button
                    onClick={() => handleReject(id)}
                    className="text-[10px] font-mono px-2 py-1 rounded border border-[color-mix(in_oklab,var(--color-status-failure)_50%,transparent)] text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] transition-colors"
                  >Reject</button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {stats && items.length === 0 && (
        <EmptyState message="No pending items in the entity review queue." />
      )}
    </section>
  );
}

// ── KGAliasPanel ──────────────────────────────────────────────────────────────
function KGAliasPanel() {
  const [canonical, setCanonical] = useState('');
  const [loading, setLoading] = useState(false);
  const [aliases, setAliases] = useState<Array<{
    alias: string; canonical: string; entity_type: string; confidence: number; source: string; created_at: string;
  }> | null>(null);

  const [regAlias, setRegAlias] = useState('');
  const [regCanonical, setRegCanonical] = useState('');
  const [regEntityType, setRegEntityType] = useState('');
  const [regResult, setRegResult] = useState<{ ok: boolean; error?: string } | null>(null);
  const [regLoading, setRegLoading] = useState(false);

  const handleList = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.kgAliasList(canonical.trim() || undefined);
      setAliases(r.aliases);
    } finally {
      setLoading(false);
    }
  }, [canonical]);

  const handleRegister = useCallback(async () => {
    if (!regAlias.trim() || !regCanonical.trim() || !regEntityType.trim()) return;
    setRegLoading(true);
    setRegResult(null);
    try {
      const r = await api.kgAliasRegister(regAlias.trim(), regCanonical.trim(), regEntityType.trim());
      setRegResult(r);
      if (r.ok) {
        setRegAlias('');
        setRegCanonical('');
        setRegEntityType('');
        handleList();
      }
    } catch (e) {
      setRegResult({ ok: false, error: (e as Error).message });
    } finally {
      setRegLoading(false);
    }
  }, [regAlias, regCanonical, regEntityType, handleList]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">KG Alias Management</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg alias list/register — Tier-2 alias table: map short names to canonical entity IDs
        </p>
      </div>

      <div className="space-y-2">
        <p className={labelClass}>List Aliases</p>
        <div className="flex gap-2">
          <input
            type="text"
            value={canonical}
            onChange={(e) => setCanonical(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleList()}
            placeholder="Filter by canonical (optional)"
            className={`flex-1 ${inputClass}`}
          />
          <button
            onClick={handleList}
            disabled={loading}
            className={clsx(
              'text-xs font-mono px-3 py-1.5 rounded border transition-colors',
              loading
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {loading ? '…' : 'List'}
          </button>
        </div>
        {aliases !== null && (
          aliases.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--color-border)] text-[var(--color-text-faint)]">
                    <th className="text-left py-1 pr-3">alias</th>
                    <th className="text-left py-1 pr-3">canonical</th>
                    <th className="text-left py-1 pr-3">type</th>
                    <th className="text-left py-1">confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {aliases.map((a) => (
                    <tr key={`${a.alias}/${a.entity_type}`} className="border-b border-[var(--color-border)] last:border-0">
                      <td className="py-1 pr-3 text-[var(--color-text)]">{a.alias}</td>
                      <td className="py-1 pr-3 text-[var(--color-text-muted)]">{a.canonical}</td>
                      <td className="py-1 pr-3 text-[var(--color-text-faint)]">{a.entity_type}</td>
                      <td className="py-1 text-[var(--color-text-faint)]">{a.confidence.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState message="No aliases found. Register one below." />
          )
        )}
      </div>

      <div className="space-y-2 border-t border-[var(--color-border)] pt-3">
        <p className={labelClass}>Register Alias</p>
        <div className="grid grid-cols-3 gap-2">
          <input type="text" value={regAlias} onChange={(e) => setRegAlias(e.target.value)} placeholder="alias (gpt4)" className={inputClass} />
          <input type="text" value={regCanonical} onChange={(e) => setRegCanonical(e.target.value)} placeholder="canonical (openai/gpt-4)" className={inputClass} />
          <input type="text" value={regEntityType} onChange={(e) => setRegEntityType(e.target.value)} placeholder="entity_type (model)" className={inputClass} />
        </div>
        <button
          onClick={handleRegister}
          disabled={regLoading || !regAlias.trim() || !regCanonical.trim() || !regEntityType.trim()}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
            regLoading || !regAlias.trim() || !regCanonical.trim() || !regEntityType.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {regLoading ? 'registering…' : 'Register'}
        </button>
        {regResult && (
          <p className={clsx(
            'text-[10px] font-mono',
            regResult.ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
          )}>
            {regResult.ok ? '✓ Alias registered' : `✗ ${regResult.error}`}
          </p>
        )}
      </div>
    </section>
  );
}
