// MCP Supply-Chain Risk Scanner panel (nova mcp scan — E-9). Extracted
// verbatim from InfraTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';

export default function MCPScanPanel() {
  const [manifestJson, setManifestJson] = useState('{\n  "name": "my-server",\n  "version": "1.0.0",\n  "tools": []\n}');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.mcpScan>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const scan = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const manifest = JSON.parse(manifestJson) as Record<string, unknown>;
      const r = await api.mcpScan(manifest);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [manifestJson]);

  const riskCls = (level: string) => {
    switch (level) {
      case 'CRITICAL':
      case 'HIGH':
        return 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]';
      case 'MEDIUM':
        return 'text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_10%,transparent)]';
      default:
        return 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]';
    }
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">MCP Supply-Chain Risk Scanner</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Scan an MCP server manifest for OWASP LLM supply-chain risks — <code className="font-mono">nova mcp scan</code>
          </p>
        </div>
        <span className="text-[var(--text-2xs)] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">E-9</span>
      </div>
      <textarea
        value={manifestJson}
        onChange={e => setManifestJson(e.target.value)}
        rows={6}
        className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none resize-y"
      />
      <button
        onClick={scan}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Scan Manifest'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className={clsx('text-[var(--text-2xs)] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded', riskCls(result.overall_risk_level))}>
              {result.overall_risk_level}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.total_findings} finding{result.total_findings !== 1 ? 's' : ''} in {result.tools.length} tool{result.tools.length !== 1 ? 's' : ''}
            </span>
          </div>
          {result.total_findings === 0 ? (
            <p className="text-[10px] text-[var(--color-status-success)]">✓ No risks detected</p>
          ) : (
            <div className="space-y-1.5 max-h-52 overflow-y-auto">
              {result.tools.filter(t => t.findings.length > 0).map(tool => (
                <div key={tool.tool_name} className="space-y-0.5">
                  <p className="text-[10px] font-mono font-semibold text-[var(--color-text)]">{tool.tool_name}</p>
                  {tool.findings.map((f, i) => (
                    <div key={i} className="flex items-start gap-1.5 text-[10px] ml-2">
                      <span className={clsx('font-mono shrink-0', riskCls(f.severity).split(' ')[0])}>
                        [{f.severity}]
                      </span>
                      <span className="text-[var(--color-text)] leading-tight">{f.message}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
