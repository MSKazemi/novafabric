// Agent edges query panel (models / tools / MCP servers reachable from an
// agent). Extracted verbatim from KGTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';
import { inputClass, labelClass, type KGEdge, type MCPServerEdge, type ModelEdge, type ToolEdge } from './shared';

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

export default function AgentQueryPanel({ runIds }: { runIds: string[] }) {
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
