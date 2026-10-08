/**
 * Risk & Assurance — surfaces the run-level safety/risk commands:
 *   nova assure        → OWASP Top 10 for LLMs evidence checks
 *   nova scan-secrets  → secret/PII findings in the redaction log
 *   nova diagnose      → failure attribution (ADR-0084)
 *   nova classify      → EU AI Act / NIST risk-tier classification
 *   nova mcp scan      → MCP supply-chain risk
 */
import { useEffect, useState } from 'react';
import type { Tab } from '../Sidebar';
import { api } from '../../../lib/api';
import { useMutation } from '../../../lib/useMutation';
import { useUrlState } from '../../../lib/useUrlState';
import TabShell from './TabShell';
import ActionButton from '../../ui/ActionButton';

function StatusChip({ status }: { status: string }) {
  const s = status.toLowerCase();
  const tone =
    s.includes('pass') || s === 'ok' ? 'var(--color-status-success)'
    : s.includes('fail') || s.includes('critical') || s.includes('high') ? 'var(--color-status-failure)'
    : 'var(--color-text-muted)';
  return <span className="font-mono text-[10px] uppercase" style={{ color: tone }}>{status}</span>;
}

function ResultCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-xs space-y-1">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{title}</div>
      {children}
    </div>
  );
}

export default function RiskTab({ onNavigate }: { onNavigate?: (t: Tab) => void }) {
  const [runId, setRunId] = useUrlState('run_id', '');
  const [vocabularies, setVocabularies] = useState<Array<{ framework: string; version: string; reference: string }>>([]);

  useEffect(() => {
    api.governanceVocabularies().then((r) => setVocabularies(r.vocabularies ?? [])).catch(() => {});
  }, []);

  const assure = useMutation((rid: string) => api.assureRun(rid), { silentSuccess: true });
  const scan = useMutation((rid: string) => api.scanSecrets(rid), { silentSuccess: true });
  const diagnose = useMutation((rid: string) => api.diagnose(rid), { silentSuccess: true });
  const classify = useMutation((rid: string) => api.classifyFromCapsule(rid), { silentSuccess: true });

  const rid = runId.trim();
  const disabled = !rid;

  return (
    <TabShell
      title="Risk & Assurance"
      subtitle="Run safety, secret-leak, failure-attribution, and risk-tier checks against a capture."
      cli={['nova assure', 'nova scan-secrets', 'nova diagnose', 'nova classify', 'nova mcp scan']}
      help="Enter a run ID, then run OWASP LLM assurance, a secret/PII scan, failure attribution, or risk-tier classification. MCP scanning takes a server manifest."
    >
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
          Run ID
          <input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            placeholder="01JABC…"
            className="w-64 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
          />
        </label>
        <ActionButton onClick={() => assure.run(rid)} pending={assure.pending} disabled={disabled} variant="primary">Assure</ActionButton>
        <ActionButton onClick={() => scan.run(rid)} pending={scan.pending} disabled={disabled}>Scan secrets</ActionButton>
        <ActionButton onClick={() => diagnose.run(rid)} pending={diagnose.pending} disabled={disabled}>Diagnose</ActionButton>
        <ActionButton onClick={() => classify.run(rid)} pending={classify.pending} disabled={disabled}>Classify</ActionButton>
        {onNavigate && (
          <ActionButton onClick={() => onNavigate('runs')} variant="ghost" size="sm">Browse runs →</ActionButton>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* Assurance */}
        {assure.result && (
          <ResultCard title="OWASP LLM Assurance">
            <div className="flex items-center gap-3 mb-1">
              <StatusChip status={assure.result.overall_status ?? (assure.result.ok ? 'ok' : 'stub')} />
              <span className="text-[10px] text-[var(--color-text-faint)]">
                {assure.result.pass_count ?? 0} pass · {assure.result.fail_count ?? 0} fail · {assure.result.warn_count ?? 0} warn
              </span>
            </div>
            {(assure.result.results ?? []).slice(0, 12).map((c) => (
              <div key={c.check_id} className="flex items-center gap-2 font-mono text-[10px]">
                <StatusChip status={c.status} />
                <span className="truncate">{c.check_id}: {c.message}</span>
              </div>
            ))}
            {assure.result.reason && <p className="text-[var(--color-text-faint)]">{assure.result.reason}</p>}
          </ResultCard>
        )}

        {/* Scan secrets */}
        {scan.result && (
          <ResultCard title="Secret / PII scan">
            <div className="flex items-center gap-3 mb-1">
              <span className={scan.result.triggered ? 'text-[var(--color-status-failure)]' : 'text-[var(--color-status-success)]'}>
                {scan.result.findings_count?.total ?? 0} findings
              </span>
              {scan.result.fail_on && <span className="text-[10px] text-[var(--color-text-faint)]">fail-on: {scan.result.fail_on}</span>}
            </div>
            {scan.result.findings.slice(0, 12).map((f, i) => (
              <div key={`${f.rule_id}-${i}`} className="flex items-center gap-2 font-mono text-[10px]">
                <StatusChip status={f.severity} />
                <span className="truncate">{f.rule_id} → {f.target_ref}</span>
              </div>
            ))}
          </ResultCard>
        )}

        {/* Diagnose */}
        {diagnose.result && (
          <ResultCard title="Failure attribution">
            {diagnose.result.ok
              ? <pre className="whitespace-pre-wrap font-mono text-[10px] text-[var(--color-text-muted)] max-h-60 overflow-auto">{JSON.stringify(diagnose.result.attribution, null, 2)}</pre>
              : <p className="text-[var(--color-status-failure)]">{diagnose.result.error}</p>}
          </ResultCard>
        )}

        {/* Classify */}
        {classify.result && (
          <ResultCard title="Risk-tier classification">
            <pre className="whitespace-pre-wrap font-mono text-[10px] text-[var(--color-text-muted)] max-h-60 overflow-auto">{JSON.stringify(classify.result.result, null, 2)}</pre>
          </ResultCard>
        )}
      </div>

      {/* Vocabularies reference */}
      <section className="rounded border border-[var(--color-border)] p-4 space-y-2">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">Classification vocabularies</h3>
        {vocabularies.length === 0 ? (
          <p className="text-xs text-[var(--color-text-faint)]">No vocabularies loaded.</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {vocabularies.map((v) => (
              <span key={`${v.framework}-${v.version}`} className="text-[10px] font-mono px-2 py-1 rounded bg-[var(--color-bg-sunken)] text-[var(--color-text-muted)]">
                {v.framework} {v.version}
              </span>
            ))}
          </div>
        )}
      </section>
    </TabShell>
  );
}
