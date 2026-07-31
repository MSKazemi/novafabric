// MCP Risk Report panel (nova mcp risk-report — structured OWASP report).
// Extracted verbatim from InfraTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';

export default function MCPRiskReportPanel() {
  const [manifestJson, setManifestJson] = useState('{\n  "name": "my-server",\n  "version": "1.0.0",\n  "tools": []\n}');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.mcpRiskReport>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const manifest = JSON.parse(manifestJson) as Record<string, unknown>;
      const r = await api.mcpRiskReport(manifest);
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
          <h3 className="text-xs font-semibold text-[var(--color-text)]">MCP Risk Report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Generate a structured OWASP LLM risk report for an MCP manifest — <code className="font-mono">nova mcp risk-report</code>
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
        onClick={generate}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Generate Report'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && !result.ok && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {result.note ?? 'Unknown error'}
        </div>
      )}
      {result && result.ok && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={clsx('text-[var(--text-2xs)] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded', riskCls(result.overall_risk_level ?? 'LOW'))}>
              {result.overall_risk_level ?? 'LOW'}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.total_findings ?? 0} finding{(result.total_findings ?? 0) !== 1 ? 's' : ''}
            </span>
            {result.server_name && (
              <span className="text-[10px] font-mono text-[var(--color-text-faint)]">· {result.server_name}</span>
            )}
          </div>
          {(result.total_findings ?? 0) === 0 ? (
            <p className="text-[10px] text-[var(--color-status-success)]">✓ No risks detected</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--color-border)] text-[var(--color-text-faint)]">
                    <th className="text-left py-1 pr-3">Tool</th>
                    <th className="text-right py-1 pr-3">Risk Score</th>
                    <th className="text-left py-1 pr-3">Highest Severity</th>
                    <th className="text-right py-1">Findings</th>
                  </tr>
                </thead>
                <tbody>
                  {(result.tools ?? []).filter(t => t.findings.length > 0).map(tool => (
                    <tr key={tool.tool_name} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-bg-sunken)]">
                      <td className="py-1 pr-3 text-[var(--color-text)] truncate max-w-[160px]">{tool.tool_name}</td>
                      <td className="py-1 pr-3 text-right text-[var(--color-text-faint)]">{tool.risk_score.toFixed(1)}</td>
                      <td className="py-1 pr-3">
                        <span className={clsx('px-1 py-0.5 rounded text-[var(--text-2xs)]', riskCls(tool.highest_severity))}>
                          {tool.highest_severity}
                        </span>
                      </td>
                      <td className="py-1 text-right text-[var(--color-text-faint)]">{tool.findings.length}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
