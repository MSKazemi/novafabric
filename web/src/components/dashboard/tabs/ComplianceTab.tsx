import { useState, useCallback, useEffect } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import type { ToolPermissionEventRecord } from '../../../lib/api';
import { SuggestInput } from '../../ui/SuggestInput';
import CopyButton from '../../ui/CopyButton';

function useLocalMru(key: string, max = 12): [string[], (v: string) => void] {
  const [mru, setMru] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem(key) ?? '[]'); } catch { return []; }
  });
  const push = useCallback((v: string) => {
    setMru(prev => {
      const next = [v, ...prev.filter(x => x !== v)].slice(0, max);
      try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* quota */ }
      return next;
    });
  }, [key, max]);
  return [mru, push];
}

// ---------- Tool Permission Events panel ----------

function PermissionBadge({ decision }: { decision: ToolPermissionEventRecord['decision'] }) {
  const cls =
    decision === 'allowed'
      ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
      : decision === 'denied'
      ? 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]'
      : 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]';
  return (
    <span className={`text-[9px] uppercase tracking-wider px-1 py-px rounded font-semibold ${cls}`}>
      {decision}
    </span>
  );
}

function ToolPermissionPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<ToolPermissionEventRecord[] | null>(null);

  const load = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setEvents(null);
    try {
      const res = await api.getToolPermissionEvents(id);
      setEvents(res.events);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId]);

  const denied = events?.filter(e => e.decision === 'denied').length ?? 0;
  const escalated = events?.filter(e => e.decision === 'escalated_to_human').length ?? 0;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Tool Permission Events</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-004 · EU AI Act Art. 9 · <code className="font-mono">nova capture</code> records per-tool decisions
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-004</span>
      </div>

      <div className="flex gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={load}
          placeholder="capsule run_id"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <button
          onClick={load}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? '…' : 'Load'}
        </button>
      </div>

      <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
        $ nova capture --run-id {runId || '<run_id>'} &amp;&amp; nova lineage provenance {runId || '<run_id>'}
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {events !== null && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 text-[10px] text-[var(--color-text-faint)]">
            <span>{events.length} event{events.length !== 1 ? 's' : ''}</span>
            {denied > 0 && <span className="text-[var(--color-status-failure)]">{denied} denied</span>}
            {escalated > 0 && <span className="text-[var(--color-status-pending)]">{escalated} escalated</span>}
          </div>
          {events.length === 0 ? (
            <p className="text-xs text-[var(--color-text-faint)] italic py-3 text-center">
              No tool permission events recorded for this capsule.
            </p>
          ) : (
            <div className="max-h-72 overflow-y-auto space-y-1 pr-1">
              {events.map((ev) => (
                <div
                  key={ev.event_id}
                  className="flex items-start gap-2 text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5"
                >
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="font-mono font-medium text-[var(--color-text)]">{ev.tool_name}</span>
                      <PermissionBadge decision={ev.decision} />
                      <span className="text-[var(--color-text-faint)]">{ev.permission_level}</span>
                    </div>
                    <div className="text-[var(--color-text-faint)] flex gap-3">
                      <span>policy: <span className="font-mono text-[var(--color-text-muted)]">{ev.policy_id}</span></span>
                      <span>latency: {ev.decision_latency_ms}ms</span>
                      {ev.human_approval_required && (
                        <span className={ev.human_approval_obtained ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-pending)]'}>
                          human-approval: {ev.human_approval_obtained ? '✓' : 'pending'}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- Annex IV export panel ----------

function AnnexIVPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [deploymentId, setDeploymentId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [deploymentSuggestions, pushDeploymentId] = useLocalMru('nova-deployment-ids');

  const run = useCallback(async () => {
    if (!runId.trim() || !deploymentId.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.exportAnnexIV(runId.trim(), deploymentId.trim());
      setResult(res);
      pushDeploymentId(deploymentId.trim());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, deploymentId, pushDeploymentId]);

  const downloadJson = useCallback(() => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `annex-iv-${deploymentId || 'export'}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result, deploymentId]);

  const complete = typeof result?.complete_elements === 'number' ? result.complete_elements as number : null;
  const total = typeof result?.total_elements === 'number' ? result.total_elements as number : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">EU AI Act Annex IV</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-002 · Regulation (EU) 2024/1689 · 15 mandatory technical documentation elements
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-002</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={run}
          placeholder="capsule run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <SuggestInput
          value={deploymentId}
          onChange={setDeploymentId}
          suggestions={deploymentSuggestions}
          onEnter={run}
          placeholder="deployment_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      </div>

      <div className="flex gap-2">
        <div className="flex-1 font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
          $ nova export-annex-iv {runId || '<capsule_dir>'} --output-dir ./out --deployment-id {deploymentId || '<id>'}
        </div>
        <button
          onClick={run}
          disabled={loading || !runId.trim() || !deploymentId.trim()}
          className="px-3 py-1 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
        >
          {loading ? '…' : 'Export'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              {complete !== null && total !== null
                ? `${complete}/${total} elements complete`
                : 'Export ready'}
            </span>
            <button
              onClick={downloadJson}
              className="text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
            >
              ↓ Download JSON-LD
            </button>
          </div>
          <pre className="text-[9px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2 max-h-48 overflow-auto whitespace-pre-wrap text-[var(--color-text-faint)]">
            {JSON.stringify(result, null, 2).slice(0, 2000)}
            {JSON.stringify(result, null, 2).length > 2000 && '\n… (truncated)'}
          </pre>
        </div>
      )}
    </section>
  );
}

// ---------- NIS2 export panel ----------

function NIS2Panel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [incidentId, setIncidentId] = useState('');
  const [phase, setPhase] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [incidentSuggestions, pushIncidentId] = useLocalMru('nova-incident-ids');

  const run = useCallback(async () => {
    if (!runId.trim() || !incidentId.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.exportNis2(runId.trim(), incidentId.trim(), phase);
      setResult(res);
      pushIncidentId(incidentId.trim());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, incidentId, phase, pushIncidentId]);

  const downloadJson = useCallback(() => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `nis2-phase${phase}-${incidentId || 'report'}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result, phase, incidentId]);

  const missingFields = typeof result?.missing_fields === 'number' ? result.missing_fields as number : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">NIS2 Incident Report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-005 · Directive (EU) 2022/2555 Art. 23 · Phase 1 ≤24h, Phase 2 ≤72h, Phase 3 ≤1 month
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-005</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={run}
          placeholder="capsule run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <SuggestInput
          value={incidentId}
          onChange={setIncidentId}
          suggestions={incidentSuggestions}
          onEnter={run}
          placeholder="incident_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      </div>

      <div className="flex items-center gap-2">
        <label className="text-[10px] text-[var(--color-text-faint)] shrink-0">Phase:</label>
        {([1, 2, 3] as const).map((p) => (
          <button
            key={p}
            onClick={() => setPhase(p)}
            className={[
              'text-[10px] px-2.5 py-1 rounded border transition-colors',
              phase === p
                ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_15%,transparent)] text-[var(--color-accent)]'
                : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)]',
            ].join(' ')}
          >
            {p}
          </button>
        ))}
        <span className="text-[10px] text-[var(--color-text-faint)]">
          {phase === 1 ? '≤24h initial' : phase === 2 ? '≤72h detailed' : '≤1 month final'}
        </span>
        <div className="flex-1" />
        <button
          onClick={run}
          disabled={loading || !runId.trim() || !incidentId.trim()}
          className="px-3 py-1 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? '…' : 'Export'}
        </button>
      </div>

      <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
        $ nova export-nis2 {runId || '<capsule_dir>'} --output ./out/nis2.json --incident-id {incidentId || '<id>'} --phase {phase}
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              Phase {phase} report generated
              {missingFields !== null && missingFields > 0 && (
                <span className="text-[var(--color-status-pending)] ml-2">· {missingFields} fields missing (cap-006 required)</span>
              )}
            </span>
            <button
              onClick={downloadJson}
              className="text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
            >
              ↓ Download JSON
            </button>
          </div>
          <pre className="text-[9px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2 max-h-48 overflow-auto whitespace-pre-wrap text-[var(--color-text-faint)]">
            {JSON.stringify(result, null, 2).slice(0, 2000)}
            {JSON.stringify(result, null, 2).length > 2000 && '\n… (truncated)'}
          </pre>
        </div>
      )}
    </section>
  );
}

// ---------- Subject Proof panel ----------

function SubjectProofPanel() {
  const [subjectId, setSubjectId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [subjectSuggestions, pushSubjectId] = useLocalMru('nova-subject-proof-ids');

  const run = useCallback(async () => {
    const id = subjectId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.subjectProof(id);
      setResult(res);
      pushSubjectId(id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [subjectId, pushSubjectId]);

  const downloadJson = useCallback(() => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `subject-proof-${subjectId || 'report'}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result, subjectId]);

  const records = Array.isArray(result?.records) ? result!.records as unknown[] : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">GDPR Subject Proof</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-001 · Art. 17 Right to Erasure · HMAC-SHA256 lookup in redaction index
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">cap-001</span>
      </div>

      <div className="flex gap-2">
        <SuggestInput
          value={subjectId}
          onChange={setSubjectId}
          suggestions={subjectSuggestions}
          onEnter={run}
          placeholder="data subject identifier (e.g. user@example.com)"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <button
          onClick={run}
          disabled={loading || !subjectId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? '…' : 'Lookup'}
        </button>
      </div>

      <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
        $ nova subject-proof {subjectId || '<subject_id>'}
      </div>

      <div className="text-[10px] text-[var(--color-text-faint)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] border border-[color-mix(in_oklab,var(--color-status-pending)_20%,transparent)] rounded px-2.5 py-1.5">
        Requires <code className="font-mono">NOVA_PII_PEPPER</code> env var set on the server. Subject ID is never stored — only an HMAC is queried.
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              {records !== null ? `${records.length} redaction record${records.length !== 1 ? 's' : ''}` : 'Proof ready'}
            </span>
            <button
              onClick={downloadJson}
              className="text-[10px] px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
            >
              ↓ Download JSON
            </button>
          </div>
          {records !== null && records.length === 0 && (
            <p className="text-xs text-[var(--color-text-faint)] italic py-2 text-center">
              No redaction records found for this subject — either not captured or already erased.
            </p>
          )}
          {records !== null && records.length > 0 && (
            <div className="max-h-48 overflow-y-auto space-y-1 pr-1">
              {(records as Array<Record<string, unknown>>).map((r, i) => (
                <div key={i} className="text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 space-y-0.5">
                  <div className="font-mono text-[var(--color-text)] truncate">{String(r.capsule_id)}</div>
                  <div className="text-[var(--color-text-faint)] flex gap-3">
                    <span>field: <span className="font-mono">{String(r.field_path)}</span></span>
                    <span>basis: {String(r.legal_basis)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- Compliance Audit panel ----------

const AUDIT_PROFILES = [
  'nist-ai-rmf',
  'eu-ai-act-high-risk',
  'gdpr',
  'soc2-type2',
  'iso42001',
  'scientific-reproducibility',
];

function AuditPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [profile, setProfile] = useState('nist-ai-rmf');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setReport(null);
    try {
      const res = await api.auditReport(id, profile);
      setReport(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, profile]);

  const overallScore = typeof report?.overall_score === 'number' ? report.overall_score as number : null;
  const totalControls = typeof report?.total_controls === 'number' ? report.total_controls as number : null;
  const coveredControls = typeof report?.covered_controls === 'number' ? report.covered_controls as number : null;
  const partialControls = typeof report?.partial_controls === 'number' ? report.partial_controls as number : null;
  const missingControls = typeof report?.missing_controls === 'number' ? report.missing_controls as number : null;
  const coverages = Array.isArray(report?.coverages) ? report!.coverages as Array<Record<string, unknown>> : null;
  const cliCmd = `nova audit report ${runId || '<capsule_dir>'} --profile ${profile}`;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Compliance Audit</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            ADR-0061 · map capsule evidence to regulatory controls
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">ADR-0061</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={run}
          placeholder="capsule run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        >
          {AUDIT_PROFILES.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>

      <div className="flex gap-2 items-center">
        <div className="flex-1 font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] truncate">
          $ {cliCmd}
        </div>
        <CopyButton text={cliCmd} label="CLI" />
        <button
          onClick={run}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
        >
          {loading ? '…' : 'Run Audit'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {report && overallScore !== null && (
        <div className="space-y-3">
          {/* Score bar */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[10px] font-mono text-[var(--color-text-faint)]">
              <span>Overall coverage</span>
              <span className={clsx(
                'font-bold',
                overallScore >= 0.8 ? 'text-[var(--color-status-success)]'
                  : overallScore >= 0.5 ? 'text-[var(--color-status-pending)]'
                  : 'text-[var(--color-status-failure)]',
              )}>
                {(overallScore * 100).toFixed(0)}%
              </span>
            </div>
            <div className="h-2 rounded-full bg-[var(--color-bg-sunken)] overflow-hidden border border-[var(--color-border)]">
              <div
                className={clsx(
                  'h-full rounded-full transition-all',
                  overallScore >= 0.8 ? 'bg-[var(--color-status-success)]'
                    : overallScore >= 0.5 ? 'bg-[var(--color-status-pending)]'
                    : 'bg-[var(--color-status-failure)]',
                )}
                style={{ width: `${Math.round(overallScore * 100)}%` }}
              />
            </div>
            <div className="flex gap-3 text-[9px] font-mono text-[var(--color-text-faint)]">
              {coveredControls !== null && <span className="text-[var(--color-status-success)]">{coveredControls} covered</span>}
              {partialControls !== null && partialControls > 0 && <span className="text-[var(--color-status-pending)]">{partialControls} partial</span>}
              {missingControls !== null && missingControls > 0 && <span className="text-[var(--color-status-failure)]">{missingControls} missing</span>}
              {totalControls !== null && <span>{totalControls} total</span>}
            </div>
          </div>

          {/* Control checklist */}
          {coverages && coverages.length > 0 && (
            <div className="max-h-64 overflow-y-auto space-y-1 pr-1">
              {coverages.map((cov, i) => {
                const status = String(cov.status ?? '');
                const statusColor =
                  status === 'covered' ? 'text-[var(--color-status-success)]'
                  : status === 'partial' ? 'text-[var(--color-status-pending)]'
                  : 'text-[var(--color-status-failure)]';
                const icon = status === 'covered' ? '✓' : status === 'partial' ? '◑' : '✗';
                return (
                  <div
                    key={i}
                    className="flex items-start gap-2 text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5"
                  >
                    <span className={clsx('shrink-0 font-mono font-bold mt-px', statusColor)}>{icon}</span>
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[var(--color-text-faint)]">{String(cov.control_id)}</span>
                        <span className="text-[var(--color-text-muted)] truncate">{String(cov.control_title)}</span>
                      </div>
                      {Array.isArray(cov.evidence_missing) && (cov.evidence_missing as unknown[]).length > 0 && (
                        <p className="text-[var(--color-status-failure)] text-[9px]">
                          Missing: {(cov.evidence_missing as string[]).join(', ')}
                        </p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- Examiner Export panel ----------

const EXAMINER_FORMATS = [
  { value: 'bagit', label: 'BagIt (RFC 8493)', desc: 'Digital preservation archive for DSpace/Archivematica' },
  { value: 'pccp', label: 'PCCP (FDA 21 CFR)', desc: 'Predetermined Change Control Plan for SaMD' },
  { value: 'iso42001', label: 'ISO/IEC 42001', desc: 'AI Management System evidence package' },
] as const;

type ExaminerFormat = typeof EXAMINER_FORMATS[number]['value'];

function ExaminerPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [format, setFormat] = useState<ExaminerFormat>('bagit');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; format: string; size_bytes: number; note: string } | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.exportExaminer(id, format);
      setResult(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, format]);

  const cliCmd = `nova export-examiner ${format} ${runId || '<capsule_dir>'} --output ./out/${runId || 'capsule'}-${format}.zip`;
  const selectedFmt = EXAMINER_FORMATS.find(f => f.value === format);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Examiner Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            ADR-0062 · BagIt · PCCP · ISO 42001 evidence packages
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">ADR-0062</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={run}
          placeholder="capsule run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <select
          value={format}
          onChange={(e) => setFormat(e.target.value as ExaminerFormat)}
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        >
          {EXAMINER_FORMATS.map((f) => (
            <option key={f.value} value={f.value}>{f.label}</option>
          ))}
        </select>
      </div>

      {selectedFmt && (
        <p className="text-[10px] text-[var(--color-text-faint)]">{selectedFmt.desc}</p>
      )}

      <div className="flex gap-2 items-center">
        <div className="flex-1 font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] truncate">
          $ {cliCmd}
        </div>
        <CopyButton text={cliCmd} label="CLI" />
        <button
          onClick={run}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
        >
          {loading ? '…' : 'Export'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result?.ok && (
        <div className="text-xs text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)] rounded px-3 py-2 space-y-1">
          <p className="font-semibold">Export generated ({(result.size_bytes / 1024).toFixed(1)} KB)</p>
          {result.note && (
            <p className="text-[10px] text-[var(--color-text-muted)]">{result.note}</p>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- Main ComplianceTab ----------

// ---------- DB-ERA-1: GDPR erasure panel (v0.18.0, cap-003) ----------

function GdprErasurePanel({ runIds }: { runIds: string[] }) {
  const [subjectId, setSubjectId] = useState('');
  const [reason, setReason] = useState('gdpr_art_17');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<{ state: string; cap003_enabled: boolean; note: string } | null>(null);
  const [erasureSuggestions, pushErasureSubject] = useLocalMru('nova-erasure-subjects');

  const [statusSubjectId, setStatusSubjectId] = useState('');
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusResult, setStatusResult] = useState<{ subject_id: string | null; cap003_enabled: boolean; requests: unknown[]; note: string } | null>(null);

  const checkStatus = useCallback(async () => {
    setStatusLoading(true);
    try { setStatusResult(await api.erasureStatus(statusSubjectId.trim() || undefined)); }
    finally { setStatusLoading(false); }
  }, [statusSubjectId]);

  const submit = useCallback(async () => {
    const id = subjectId.trim();
    if (!id) return;
    setSubmitting(true);
    setErr(null);
    try {
      const r = await api.erasureRequest(id, reason);
      setResult({ state: r.state, cap003_enabled: r.cap003_enabled, note: r.note });
      pushErasureSubject(id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [subjectId, reason, pushErasureSubject]);

  const stateColor: Record<string, string> = {
    PENDING: 'text-[var(--color-status-pending)]',
    FEATURE_FLAG_OFF: 'text-[var(--color-text-muted)]',
    TOMBSTONED: 'text-[var(--color-status-success)]',
    FAILED: 'text-[var(--color-status-failure)]',
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">GDPR Erasure Request</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-003 · Art. 17 right-to-be-forgotten · dual-object split (audit/PII)
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
          ADR-0066
        </span>
      </div>

      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2 font-mono">{err}</div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Subject ID (or Run ID)</label>
          <SuggestInput
            value={subjectId}
            onChange={setSubjectId}
            suggestions={[...new Set([...runIds, ...erasureSuggestions])]}
            onEnter={submit}
            placeholder="subject-001 or run_..."
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono w-full"
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Reason</label>
          <select
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono w-full"
          >
            <option value="gdpr_art_17">gdpr_art_17 (right-to-be-forgotten)</option>
            <option value="dsar">dsar (data subject access request)</option>
            <option value="internal">internal</option>
          </select>
        </div>
      </div>

      <button
        onClick={submit}
        disabled={submitting || !subjectId.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          submitting || !subjectId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {submitting ? 'submitting…' : 'Queue erasure request'}
      </button>

      {result && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 space-y-1.5">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">state</span>
            <span className={clsx('text-xs font-mono font-bold', stateColor[result.state] ?? 'text-[var(--color-text)]')}>{result.state}</span>
            {!result.cap003_enabled && (
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] text-[var(--color-status-pending)]">
                NOVA_CAP003_ENABLED=false
              </span>
            )}
          </div>
          <p className="text-[10px] text-[var(--color-text-muted)]">{result.note}</p>
        </div>
      )}

      <div className="border-t border-[var(--color-border)] pt-3 space-y-2">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Check Erasure Status</p>
        <p className="text-[10px] text-[var(--color-text-faint)]">nova erasure status — list pending/completed erasure requests for a subject</p>
        <div className="flex gap-2">
          <SuggestInput
            value={statusSubjectId}
            onChange={setStatusSubjectId}
            suggestions={erasureSuggestions}
            onEnter={checkStatus}
            placeholder="subject-001 (leave blank for all)"
            className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
          <button
            onClick={checkStatus}
            disabled={statusLoading}
            className={clsx(
              'text-xs font-mono px-3 py-1.5 rounded border transition-colors',
              statusLoading
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {statusLoading ? '…' : 'Check Status'}
          </button>
        </div>
        {statusResult && (
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-xs font-mono space-y-1">
            <div>Subject: {statusResult.subject_id ?? '(all)'} | cap003: {statusResult.cap003_enabled ? 'enabled' : 'disabled'}</div>
            <div>Requests: {statusResult.requests.length}</div>
            {statusResult.requests.length > 0 && (
              <div className="space-y-0.5 mt-1">
                {statusResult.requests.map((r, i) => (
                  <div key={i} className="text-[10px] text-[var(--color-text-muted)]">
                    {JSON.stringify(r)}
                  </div>
                ))}
              </div>
            )}
            <div className="text-[10px] text-[var(--color-text-faint)]">{statusResult.note}</div>
          </div>
        )}
      </div>
    </section>
  );
}

export default function ComplianceTab({ runIds: initialRunIds }: { runIds?: string[] }) {
  const [fetchedRunIds, setFetchedRunIds] = useState<string[]>([]);
  useEffect(() => {
    api.listRuns().then(r => setFetchedRunIds(r.runs.map(run => run.run_id))).catch(() => {});
  }, []);
  const ids = [...new Set([...(initialRunIds ?? []), ...fetchedRunIds])];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-[var(--color-text)]">Compliance</h2>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            EU AI Act · NIS2 · GDPR · NIST AI RMF — evidence-grade exports from Run Capsules
          </p>
        </div>
        <div className="flex gap-1.5">
          {(['ADR-0054', 'ADR-0055', 'ADR-0057', 'ADR-0061', 'ADR-0066'] as const).map(label => (
            <span
              key={label}
              className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      <ToolPermissionPanel runIds={ids} />
      <AnnexIVPanel runIds={ids} />
      <NIS2Panel runIds={ids} />
      <SubjectProofPanel />
      <GdprErasurePanel runIds={ids} />
      <AuditPanel runIds={ids} />
      <ExaminerPanel runIds={ids} />
      <AuditCoveragePanel />
      <AuditBundlePanel />
      <AuditVerifyPanel />
      <AuditMapPanel />
      <AssurancePanel runIds={ids} />
      <RoCrateExportPanel runIds={ids} />
      <C2paExportPanel runIds={ids} />
      <RoPAExportPanel runIds={ids} />
      <AIBOMExportPanel runIds={ids} />
      <NISTRMFExportPanel runIds={ids} />
      <AIBOMStatusPanel />
      <AibomGeneratePanel runIds={ids} />
      <PiiErasePanel runIds={ids} />
      <HIPAAProofPanel runIds={ids} />
    </div>
  );
}

// ---------- Audit Coverage panel (nova audit coverage) ----------

type CoverageEntry = {
  control_id: string;
  control_name?: string;
  status: string;
  capsule_count?: number;
  evidence_count?: number;
  score?: number;
};

function AuditCoveragePanel() {
  const PROFILES = ['nist-ai-rmf', 'eu-ai-act', 'iso-42001'];
  const [profile, setProfile] = useState('nist-ai-rmf');
  const [threshold, setThreshold] = useState(0.8);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    profile: string;
    overall_score: number;
    threshold: number;
    threshold_met: boolean;
    capsule_count: number;
    total_controls: number;
    covered_controls: number;
    partial_controls: number;
    missing_controls: number;
    coverages: CoverageEntry[];
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.auditCoverage(profile, threshold);
      setResult({ ...r, coverages: r.coverages as CoverageEntry[] });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [profile, threshold]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Audit Coverage</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Measure how many controls your run capsules cover — <code className="font-mono">nova audit coverage</code>
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2 items-end">
        <label className="block space-y-1">
          <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Profile</span>
          <select
            value={profile}
            onChange={e => setProfile(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          >
            {PROFILES.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label className="block space-y-1">
          <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Threshold</span>
          <input
            type="number"
            min={0} max={1} step={0.05}
            value={threshold}
            onChange={e => setThreshold(parseFloat(e.target.value))}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono w-20"
          />
        </label>
        <button
          onClick={run}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 transition-colors"
        >
          {loading ? '…' : 'Check coverage'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-4 text-[10px] font-mono">
            <span>
              <span className="text-[var(--color-text-faint)]">score </span>
              <span className={clsx('font-bold', result.threshold_met ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
                {(result.overall_score * 100).toFixed(1)}%
              </span>
              <span className="text-[var(--color-text-faint)]"> (threshold {(result.threshold * 100).toFixed(0)}%)</span>
            </span>
            <span className="text-[var(--color-text-faint)]">{result.total_controls} controls · {result.covered_controls} covered · {result.partial_controls} partial · {result.missing_controls} missing</span>
            <span className="text-[var(--color-text-faint)]">{result.capsule_count} capsules analysed</span>
          </div>
          {result.coverages.length > 0 && (
            <div className="rounded border border-[var(--color-border)] overflow-hidden max-h-64 overflow-y-auto">
              <table className="w-full text-[10px] font-mono">
                <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)] sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Control</th>
                    <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Status</th>
                    <th className="text-right px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Capsules</th>
                    <th className="text-right px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Score</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--color-border)]">
                  {result.coverages.map((c) => (
                    <tr key={c.control_id} className="hover:bg-[var(--color-bg-sunken)] transition-colors">
                      <td className="px-3 py-1.5 text-[var(--color-text)]">{c.control_id}{c.control_name ? ` — ${c.control_name}` : ''}</td>
                      <td className="px-3 py-1.5">
                        <span className={clsx(
                          'text-[9px] uppercase tracking-wider px-1 py-px rounded',
                          c.status === 'covered' ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                          : c.status === 'partial' ? 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]'
                          : 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]',
                        )}>{c.status}</span>
                      </td>
                      <td className="px-3 py-1.5 text-right text-[var(--color-text-faint)]">{c.capsule_count ?? '—'}</td>
                      <td className="px-3 py-1.5 text-right text-[var(--color-text-faint)]">
                        {c.score !== undefined ? `${(c.score * 100).toFixed(0)}%` : '—'}
                      </td>
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

// ---------- Audit Bundle panel (nova audit bundle) ----------

function AuditBundlePanel() {
  const PROFILES = ['nist-ai-rmf', 'eu-ai-act', 'iso-42001'];
  const [profile, setProfile] = useState('nist-ai-rmf');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; profile: string; report_id: string; overall_score: number; filename: string; content_base64: string; size_bytes: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.auditBundle(profile);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [profile]);

  const download = useCallback(() => {
    if (!result) return;
    const bytes = atob(result.content_base64);
    const arr = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
    const blob = new Blob([arr], { type: 'application/zip' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = result.filename; a.click();
    URL.revokeObjectURL(url);
  }, [result]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Audit Bundle Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Package audit report + evidence into a ZIP — <code className="font-mono">nova audit bundle</code>
          </p>
        </div>
      </div>
      <div className="flex gap-2 items-end">
        <label className="block space-y-1">
          <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Profile</span>
          <select
            value={profile}
            onChange={e => setProfile(e.target.value)}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          >
            {PROFILES.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <button
          onClick={run}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 transition-colors"
        >
          {loading ? 'generating…' : 'Generate bundle'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="flex items-center gap-3 rounded border border-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)] px-3 py-2">
          <span className="text-[var(--color-status-success)] text-sm">↓</span>
          <div className="flex-1 min-w-0">
            <p className="text-xs font-mono text-[var(--color-text)] truncate">{result.filename}</p>
            <p className="text-[10px] text-[var(--color-text-faint)]">
              {(result.size_bytes / 1024).toFixed(1)} KB · score {(result.overall_score * 100).toFixed(1)}% · {result.profile}
            </p>
          </div>
          <button
            onClick={download}
            className="px-2.5 py-1 text-[10px] rounded border border-[var(--color-status-success)] text-[var(--color-status-success)] hover:bg-[color-mix(in_oklab,var(--color-status-success)_15%,transparent)] transition-colors font-mono"
          >download</button>
        </div>
      )}
    </section>
  );
}

// ---------- Audit Verify panel (nova audit verify) ----------

function AuditVerifyPanel() {
  const [reportJson, setReportJson] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; valid: boolean; report_id?: string; profile?: string; overall_score?: number; errors: string[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const verify = useCallback(async () => {
    const text = reportJson.trim();
    if (!text) return;
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const parsed = JSON.parse(text);
      const r = await api.auditVerify(parsed);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [reportJson]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Audit Report Verify</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          Validate an AuditReport JSON against the schema — <code className="font-mono">nova audit verify</code>
        </p>
      </div>
      <textarea
        value={reportJson}
        onChange={e => setReportJson(e.target.value)}
        placeholder='{"report_id": "...", "profile": "nist-ai-rmf", ...}'
        rows={5}
        className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
      />
      <button
        onClick={verify}
        disabled={loading || !reportJson.trim()}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 transition-colors"
      >
        {loading ? '…' : 'Verify'}
      </button>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <span className={clsx(
              'text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded',
              result.valid
                ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                : 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]',
            )}>
              {result.valid ? 'valid' : 'invalid'}
            </span>
            {result.report_id && <span className="text-[10px] font-mono text-[var(--color-text-faint)]">{result.report_id}</span>}
            {result.profile && <span className="text-[10px] font-mono text-[var(--color-text-faint)]">{result.profile}</span>}
            {result.overall_score !== undefined && (
              <span className="text-[10px] font-mono text-[var(--color-text-faint)]">{(result.overall_score * 100).toFixed(1)}%</span>
            )}
          </div>
          {result.errors.length > 0 && (
            <ul className="space-y-0.5">
              {result.errors.map((e, i) => (
                <li key={i} className="text-[10px] font-mono text-[var(--color-status-failure)]">• {e}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- Audit Control Map panel (nova audit map --profile) ----------

const AUDIT_MAP_PROFILES = ['nist-ai-rmf', 'eu-ai-act', 'iso-42001', 'nis2'];

function AuditMapPanel() {
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

// ---------- OWASP LLM Assurance panel (nova assure — E-10) ----------

function AssurancePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [capsulePath, setCapsulePath] = useState('');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.assureRun>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.assureRun(id, capsulePath.trim() || undefined);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, capsulePath]);

  const statusCls = (s: string) => {
    switch (s) {
      case 'PASS': return 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]';
      case 'FAIL': return 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]';
      case 'WARN': return 'text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_10%,transparent)]';
      default: return 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]';
    }
  };

  const statusIcon = (s: string) => {
    switch (s) {
      case 'PASS': return '✓';
      case 'FAIL': return '✗';
      case 'WARN': return '!';
      default: return '~';
    }
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">OWASP LLM Assurance Check</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Run 10 OWASP Top 10 for LLM (2025) evidence checks — <code className="font-mono">nova assure</code>
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">E-10</span>
      </div>
      <div className="space-y-1.5">
        <SuggestInput
          value={runId}
          onChange={setRunId}
          suggestions={runIds}
          onEnter={run}
          placeholder="Run ID (e.g. 01ABC123…)"
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <input
          value={capsulePath}
          onChange={e => setCapsulePath(e.target.value)}
          placeholder="Capsule path (optional — auto-resolved from run ID if blank)"
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      </div>
      <button
        onClick={run}
        disabled={loading || !runId.trim()}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Run Assurance Checks'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && result.ok === false && (
        <p className="text-xs text-[var(--color-text-faint)] font-mono">{result.reason ?? 'Capsule not found'}</p>
      )}
      {result && result.overall_status && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className={clsx('text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded', statusCls(result.overall_status))}>
              {result.overall_status}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.pass_count}P / {result.warn_count}W / {result.fail_count}F / {result.skip_count}S
            </span>
          </div>
          <div className="space-y-0.5 max-h-52 overflow-y-auto">
            {result.results?.map(r => (
              <div key={r.check_id} className="flex items-start gap-1.5 text-[10px]">
                <span className={clsx('font-mono shrink-0 w-4', statusCls(r.status).split(' ')[0])}>
                  {statusIcon(r.status)}
                </span>
                <span className="font-mono text-[var(--color-text-faint)] shrink-0 w-16">{r.check_id}</span>
                <span className="text-[var(--color-text)] leading-tight">{r.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

// ---------- RO-Crate export panel (nova export-rocrate) ----------

function RoCrateExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    run_id: string;
    filename: string;
    zip_base64: string;
    size_bytes: number;
    note: string;
  } | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportRoCrate(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  function handleDownload() {
    if (!result) return;
    const blob = new Blob([Uint8Array.from(atob(result.zip_base64), c => c.charCodeAt(0))], { type: 'application/zip' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = result.filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">RO-Crate Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export W3C RO-Crate v1.1 archive — mirrors <code className="font-mono">nova export-rocrate</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0073
        </span>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Exporting…' : 'Export RO-Crate'}
        </button>
      </div>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        W3C RO-Crate v1.1 — portable, interoperable capsule archive
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-[10px] text-[var(--color-text-faint)] font-mono">{result.filename}</span>
            <span className="text-[10px] text-[var(--color-text-faint)]">{(result.size_bytes / 1024).toFixed(1)} KB</span>
            <button
              type="button"
              onClick={handleDownload}
              className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] transition-colors"
            >
              Download ZIP
            </button>
          </div>
          {result.note && (
            <p className="text-[10px] text-[var(--color-text-faint)]">{result.note}</p>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- C2PA manifest export panel (nova export-c2pa) ----------

function C2paExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [includeTrainingMining, setIncludeTrainingMining] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    ok: boolean;
    run_id: string;
    manifest: object;
    note: string;
  } | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportC2pa(id, includeTrainingMining)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">C2PA Manifest Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export C2PA v2.3 content provenance manifest — mirrors <code className="font-mono">nova export-c2pa</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0074
        </span>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Exporting…' : 'Export C2PA Manifest'}
        </button>
      </div>

      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={includeTrainingMining}
          onChange={e => setIncludeTrainingMining(e.target.checked)}
          className="rounded"
        />
        <span className="text-xs text-[var(--color-text-muted)]">Include training-mining:notAllowed assertion</span>
      </label>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        C2PA v2.3 — ADR-0074 / EU AI Act Art.50 · deadline 2026-08-02
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">run_id: <span className="font-mono">{result.run_id}</span></span>
            <CopyButton text={JSON.stringify(result.manifest, null, 2)} />
          </div>
          <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded p-2 overflow-auto max-h-64 whitespace-pre-wrap break-all">
            {JSON.stringify(result.manifest, null, 2)}
          </pre>
          {result.note && (
            <p className="text-[10px] text-[var(--color-text-faint)]">{result.note}</p>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- GDPR RoPA export panel (nova export-ropa — cap-007) ----------

function RoPAExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [controllerName, setControllerName] = useState('');
  const [controllerContact, setControllerContact] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportRopa>> | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportRopa(id, controllerName.trim(), controllerContact.trim())
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">GDPR Art.30 RoPA Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export Records of Processing Activities — mirrors <code className="font-mono">nova export-ropa</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          cap-007
        </span>
      </div>

      <div className="space-y-2">
        <SuggestInput
          value={runId}
          onChange={setRunId}
          suggestions={runIds}
          placeholder="run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
        />
        <input
          type="text"
          value={controllerName}
          onChange={e => setControllerName(e.target.value)}
          placeholder="Controller name (GDPR Art.30(1)(a)) — optional"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 focus:border-[var(--color-accent)] focus:outline-none w-full"
        />
        <input
          type="text"
          value={controllerContact}
          onChange={e => setControllerContact(e.target.value)}
          placeholder="Controller contact details — optional"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 focus:border-[var(--color-accent)] focus:outline-none w-full"
        />
      </div>

      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] text-[var(--color-text-faint)]">
          GDPR Art.30 · cap-007 · Missing fields marked OPERATOR_DECLARED_REQUIRED
        </p>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Exporting…' : 'Export RoPA'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <span className={clsx(
              'text-[10px] font-semibold px-1.5 py-0.5 rounded',
              result.completeness === 'complete'
                ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                : 'text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_10%,transparent)]',
            )}>
              {result.completeness}
            </span>
            {result.missing_fields.length > 0 && (
              <span className="text-[10px] text-[var(--color-text-faint)]">
                missing: {result.missing_fields.join(', ')}
              </span>
            )}
            <CopyButton text={JSON.stringify(result.document, null, 2)} />
          </div>
          <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded p-2 overflow-auto max-h-64 whitespace-pre-wrap break-all">
            {JSON.stringify(result.document, null, 2)}
          </pre>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-ropa {runId} --output ropa.json{controllerName ? ` --controller-name "${controllerName}"` : ''}
          </p>
        </div>
      )}
    </section>
  );
}

// ---------- AI-SBOM export panel (nova export-aibom — cap-008) ----------

function AIBOMExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportAibom>> | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportAibom(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">AI-SBOM Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export CycloneDX 1.6 ML-BOM — mirrors <code className="font-mono">nova export-aibom</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          cap-008
        </span>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Exporting…' : 'Export AI-SBOM'}
        </button>
      </div>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        CycloneDX ML-BOM 1.6 (ECMA-424 2nd Edition) · EU CRA deadline 2026-09-11
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-[10px] text-[var(--color-text-faint)]">
              {result.component_count} component{result.component_count !== 1 ? 's' : ''} · {result.bom_format}
            </span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>
          <div className="space-y-1">
            {result.components.map((comp, i) => (
              <div key={i} className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded px-2 py-1 flex items-center gap-2">
                <span className="text-[var(--color-text-faint)] uppercase tracking-wider text-[8px]">{comp.type}</span>
                <span className="font-semibold">{comp.name}</span>
                {comp.version && <span className="text-[var(--color-text-muted)]">@{comp.version}</span>}
                {comp.description && <span className="text-[var(--color-text-faint)] truncate max-w-xs">{comp.description}</span>}
              </div>
            ))}
          </div>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            serial: {result.serial_number} · generated: {result.generated_at}
          </p>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-aibom {runId} --output aibom.json
          </p>
        </div>
      )}
    </section>
  );
}

// ---------- NIST AI RMF report panel (nova export-nist-rmf — cap-009) ----------

function NISTRMFExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportNistRmf>> | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportNistRmf(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  const riskColor = (level: string) =>
    level === 'low' ? 'var(--color-status-success)'
    : level === 'medium' ? 'var(--color-status-warning)'
    : 'var(--color-status-failure)';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">NIST AI RMF Report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Quantitative risk report (GOVERN/MAP/MEASURE/MANAGE) — mirrors <code className="font-mono">nova export-nist-rmf</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          cap-009
        </span>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Generating…' : 'Generate RMF Report'}
        </button>
      </div>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        NIST AI 100-1 (January 2023) — GOVERN · MAP · MEASURE · MANAGE
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <span
              className="text-sm font-mono font-bold"
              style={{ color: riskColor(result.risk_level) }}
            >
              {result.overall_score !== null ? result.overall_score.toFixed(2) : 'n/a'}
            </span>
            <span
              className="text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wider"
              style={{
                color: riskColor(result.risk_level),
                background: `color-mix(in oklab, ${riskColor(result.risk_level)} 12%, transparent)`,
              }}
            >
              {result.risk_level}
            </span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>
          <div className="space-y-1">
            {(['GOVERN', 'MAP', 'MEASURE', 'MANAGE'] as const).map(fn => {
              const fnMetrics = result.metrics.filter(m => m.function === fn);
              if (fnMetrics.length === 0) return null;
              const avg = fnMetrics.reduce((s, m) => s + (m.score ?? 0), 0) / fnMetrics.length;
              return (
                <div key={fn} className="flex items-center gap-2 text-[10px]">
                  <span className="font-mono font-semibold w-16 text-[var(--color-text)]">{fn}</span>
                  <div className="flex-1 h-1.5 rounded bg-[var(--color-bg-sunken)]">
                    <div
                      className="h-1.5 rounded transition-all"
                      style={{
                        width: `${avg * 100}%`,
                        background: riskColor(avg >= 0.7 ? 'low' : avg >= 0.4 ? 'medium' : 'high'),
                      }}
                    />
                  </div>
                  <span className="w-8 text-right text-[var(--color-text-muted)]">{(avg * 100).toFixed(0)}%</span>
                </div>
              );
            })}
          </div>
          {result.missing_evidence.length > 0 && (
            <div className="text-[10px] text-[var(--color-text-faint)]">
              Missing evidence: {result.missing_evidence.join(', ')}
            </div>
          )}
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-nist-rmf {runId} --output nist-rmf.json
          </p>
        </div>
      )}
    </section>
  );
}

// ---------- AIBOM status panel (nova aibom status — cap-008) ----------

function AIBOMStatusPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.aibomStatus>> | null>(null);

  function handleLoad() {
    setLoading(true);
    setError(null);
    api.aibomStatus()
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(() => { handleLoad(); }, []);

  const coverageColor = result
    ? result.coverage_status === 'complete' ? 'var(--color-status-success)'
    : result.coverage_status === 'no_capsules' ? 'var(--color-text-faint)'
    : 'var(--color-status-warning)'
    : undefined;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">AI-SBOM Coverage Status</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            CRA SBOM compliance coverage — mirrors <code className="font-mono">nova aibom status</code>
          </p>
        </div>
        <button
          type="button"
          onClick={handleLoad}
          disabled={loading}
          className="text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-40 transition-colors shrink-0"
        >
          {loading ? '…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div className="text-lg font-mono font-bold text-[var(--color-text)]">{result.total_capsules}</div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">total</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div className="text-lg font-mono font-bold" style={{ color: 'var(--color-status-success)' }}>
                {result.capsules_with_aibom}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">with AIBOM</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div
                className="text-lg font-mono font-bold"
                style={{ color: result.capsules_missing_aibom > 0 ? 'var(--color-status-warning)' : 'var(--color-text-faint)' }}
              >
                {result.capsules_missing_aibom}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">missing</div>
            </div>
          </div>

          {result.total_capsules > 0 && (
            <div className="space-y-1">
              <div className="h-2 rounded bg-[var(--color-bg-sunken)] overflow-hidden">
                <div
                  className="h-2 rounded transition-all"
                  style={{
                    width: `${(result.capsules_with_aibom / result.total_capsules) * 100}%`,
                    background: coverageColor,
                  }}
                />
              </div>
              <div className="flex justify-between text-[9px] text-[var(--color-text-faint)]">
                <span style={{ color: coverageColor }} className="font-semibold uppercase tracking-wider">
                  {result.coverage_status}
                </span>
                <span>{Math.round((result.capsules_with_aibom / result.total_capsules) * 100)}% covered</span>
              </div>
            </div>
          )}

          <div className="text-[10px] space-y-0.5 text-[var(--color-text-faint)]">
            <div><span className="font-medium">Regulation:</span> {result.regulation}</div>
            <div><span className="font-medium">CRA deadline:</span> <span className="font-mono text-[var(--color-status-failure)]">{result.cra_deadline}</span></div>
            <div><span className="font-medium">Format:</span> {result.spec_version}</div>
          </div>

          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova aibom status
          </p>
        </div>
      )}
    </section>
  );
}

// ---------- AIBOM generate panel (nova aibom generate — cap-008) ----------

function AibomGeneratePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [all, setAll] = useState(false);
  const [force, setForce] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.aibomGenerate>> | null>(null);

  function handleGenerate() {
    const id = runId.trim();
    if (!all && !id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.aibomGenerate(all ? { all: true, force } : { run_id: id, force })
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Generate AI-SBOM</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Write CycloneDX 1.7 aibom.json per capsule (EU CRA, deadline 2026-09-11) — <code className="font-mono">nova aibom generate</code>
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          cap-008
        </span>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            disabled={all}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full disabled:opacity-40 disabled:cursor-not-allowed"
          />
        </div>
        <button
          type="button"
          onClick={handleGenerate}
          disabled={loading || (!all && !runId.trim())}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Generating…' : 'Generate'}
        </button>
      </div>

      <div className="flex gap-4">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={all}
            onChange={e => setAll(e.target.checked)}
            className="rounded"
          />
          <span className="text-xs text-[var(--color-text-muted)]">All capsules</span>
        </label>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={force}
            onChange={e => setForce(e.target.checked)}
            className="rounded"
          />
          <span className="text-xs text-[var(--color-text-muted)]">Force regenerate</span>
        </label>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div className="text-lg font-mono font-bold" style={{ color: 'var(--color-status-success)' }}>
                {result.written}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">written</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div className="text-lg font-mono font-bold text-[var(--color-text-faint)]">{result.skipped}</div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">skipped</div>
            </div>
            <div className="bg-[var(--color-bg-sunken)] rounded p-2 text-center">
              <div
                className="text-lg font-mono font-bold"
                style={{ color: result.failed > 0 ? 'var(--color-status-failure)' : 'var(--color-text-faint)' }}
              >
                {result.failed}
              </div>
              <div className="text-[9px] text-[var(--color-text-faint)] uppercase tracking-wider">failed</div>
            </div>
          </div>

          {result.path && (
            <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
              path: {result.path}
            </p>
          )}

          {result.errors && result.errors.length > 0 && (
            <div className="space-y-1">
              {result.errors.map((err, i) => (
                <div
                  key={i}
                  className="text-[10px] font-mono text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] rounded px-2 py-1"
                >
                  {err}
                </div>
              ))}
            </div>
          )}

          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova aibom generate{all ? ' --all' : runId.trim() ? ` ${runId.trim()}` : ''}{force ? ' --force' : ''}
          </p>
        </div>
      )}
    </section>
  );
}

// ---------- PII DEK crypto-shredding panel (nova pii erase — ADR-0069) ----------

function PiiErasePanel({ runIds }: { runIds: string[] }) {
  const [subjectId, setSubjectId] = useState('');
  const [retentionMonths, setRetentionMonths] = useState(6);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [noDek, setNoDek] = useState(false);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.piiErase>> | null>(null);
  const [mruSubjects, pushSubject] = useLocalMru('nova-pii-subjects');

  function handleErase() {
    const id = subjectId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setNoDek(false);
    setResult(null);
    api.piiErase(id, [], retentionMonths)
      .then(r => {
        if (!r.ok && r.error === 'no DEK found') {
          setNoDek(true);
        } else {
          pushSubject(id);
          setResult(r);
        }
      })
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  const receipt = result?.receipt as Record<string, unknown> | undefined;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">DEK Crypto-Shredding (nova pii erase) — ADR-0069</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            GDPR Art.17 erasure via AES-256-GCM DEK destruction — ciphertext becomes permanently unrecoverable
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0069
        </span>
      </div>

      <div className="flex gap-2 items-end flex-wrap">
        <div className="flex-1 min-w-[180px]">
          <label className="text-[9px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1 block">Subject ID</label>
          <SuggestInput
            value={subjectId}
            onChange={setSubjectId}
            suggestions={[...mruSubjects, ...runIds]}
            placeholder="data subject identifier"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <div className="w-44">
          <label className="text-[9px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1 block">
            Retention window (months, Art.17(3)(b))
          </label>
          <input
            type="number"
            min={1}
            value={retentionMonths}
            onChange={e => setRetentionMonths(Math.max(1, Number(e.target.value)))}
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleErase}
          disabled={loading || !subjectId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-status-failure)] text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_18%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap self-end"
        >
          {loading ? 'Erasing…' : 'Erase DEK'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {noDek && (
        <div className="text-xs text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-warning)_25%,transparent)] rounded px-3 py-2">
          No DEK registered for this subject (subject may not have encrypted PII or was already erased)
        </div>
      )}

      {result?.ok && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wider"
              style={{
                color: result.erased ? 'var(--color-status-success)' : 'var(--color-status-warning)',
                background: result.erased
                  ? 'color-mix(in oklab, var(--color-status-success) 12%, transparent)'
                  : 'color-mix(in oklab, var(--color-status-warning) 12%, transparent)',
              }}
            >
              {result.erased ? 'Erased immediately' : 'Deferred'}
            </span>
            <CopyButton text={JSON.stringify(result.receipt, null, 2)} />
          </div>
          <div className="text-[10px] space-y-0.5 text-[var(--color-text-faint)]">
            {result.erased && receipt?.erased_at != null && (
              <div><span className="font-medium">Erased at:</span> <span className="font-mono">{String(receipt.erased_at)}</span></div>
            )}
            {!result.erased && receipt?.earliest_erasure_at != null && (
              <div><span className="font-medium">Deferred until:</span> <span className="font-mono">{String(receipt.earliest_erasure_at)}</span></div>
            )}
            {receipt?.proof_digest != null && (
              <div><span className="font-medium">Proof digest:</span> <span className="font-mono break-all">{String(receipt.proof_digest)}</span></div>
            )}
          </div>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova pii erase {subjectId}{retentionMonths !== 6 ? ` --retention-months ${retentionMonths}` : ''}
          </p>
        </div>
      )}
    </section>
  );
}

// ---------- HIPAA Safe Harbor proof panel (nova export-hipaa-proof) ----------

function HIPAAProofPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.exportHipaaProof>> | null>(null);

  function handleGenerate() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportHipaaProof(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  const statusColor = (status: string) =>
    status === 'not_present' || status === 'redacted'
      ? 'var(--color-status-success)'
      : status === 'not_assessable'
      ? 'var(--color-status-warning)'
      : 'var(--color-status-failure)';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">HIPAA Safe Harbor Proof (nova export-hipaa-proof)</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            45 CFR §164.514(b)(2) — technical evidence record for all 18 Safe Harbor identifier categories
          </p>
        </div>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleGenerate}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Generating…' : 'Generate Proof'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result?.ok && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-[10px] font-semibold text-[var(--color-text)]">
              {result.identifier_count}/18 identifier categories assessed
            </span>
            <span className="text-[9px] text-[var(--color-text-faint)] font-mono">{result.assessed_at}</span>
            <CopyButton text={JSON.stringify(result, null, 2)} />
          </div>
          <div className="bg-[var(--color-bg-sunken)] rounded px-3 py-2">
            <p className="text-[10px] font-mono break-all text-[var(--color-text-muted)]">{result.proof_digest}</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] border-collapse">
              <thead>
                <tr className="border-b border-[var(--color-border)]">
                  <th className="text-left py-1 pr-3 font-medium text-[var(--color-text-faint)] uppercase tracking-wider">Category</th>
                  <th className="text-left py-1 pr-3 font-medium text-[var(--color-text-faint)] uppercase tracking-wider">Status</th>
                  <th className="text-left py-1 font-medium text-[var(--color-text-faint)] uppercase tracking-wider">Evidence Source</th>
                </tr>
              </thead>
              <tbody>
                {result.categories.map(cat => (
                  <tr key={cat.id} className="border-b border-[var(--color-border)] border-opacity-50">
                    <td className="py-1 pr-3 text-[var(--color-text-muted)]">{cat.name}</td>
                    <td className="py-1 pr-3">
                      <span
                        className="px-1 py-0.5 rounded text-[9px] font-mono font-semibold uppercase tracking-wider"
                        style={{
                          color: statusColor(cat.status),
                          background: `color-mix(in oklab, ${statusColor(cat.status)} 12%, transparent)`,
                        }}
                      >
                        {cat.status}
                      </span>
                    </td>
                    <td className="py-1 text-[var(--color-text-faint)] font-mono">{cat.evidence_source ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] italic text-[var(--color-text-faint)]">{result.disclaimer}</p>
          <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
            $ nova export-hipaa-proof {runId || '&lt;capsule_dir&gt;'} --output hipaa-proof.json
          </p>
        </div>
      )}
    </section>
  );
}
