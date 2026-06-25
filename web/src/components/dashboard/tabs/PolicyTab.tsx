import { useState, useEffect, useCallback } from 'react';
import { clsx } from 'clsx';
import { api, type PolicyInput, type PolicyDecision } from '../../../lib/api';
import { SuggestInput } from '../../ui/SuggestInput';
import CopyButton from '../../ui/CopyButton';

// DB-CAP-1 — Capture-level policy panel (v0.18.0, cap-004)
function CaptureLevelPanel() {
  const [data, setData] = useState<{ current_level: string; env_var: string; tiers: Record<string, string[]>; note: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ instruction: string; note: string } | null>(null);

  useEffect(() => {
    api.captureLevelGet()
      .then((r) => { setData(r); setSelected(r.current_level); })
      .catch((e) => setErr((e as Error).message));
  }, []);

  const submit = useCallback(async () => {
    if (!selected || selected === data?.current_level) return;
    setSubmitting(true);
    setErr(null);
    try {
      const r = await api.captureLevelSet(selected);
      setResult({ instruction: r.instruction, note: r.note });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [selected, data]);

  const levelColor: Record<string, string> = {
    minimal:    'text-[var(--color-status-success)]',
    standard:   'text-[var(--color-accent)]',
    forensic:   'text-[var(--color-status-pending)]',
    air_gapped: 'text-[var(--color-status-failure)]',
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Capture Level Policy</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            cap-004 · controls which fields the capture pipeline records
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
          ADR-0066
        </span>
      </div>

      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2 font-mono">{err}</div>
      )}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 items-end">
            <div className="space-y-1">
              <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Current level</p>
              <span className={clsx('text-sm font-mono font-bold', levelColor[data.current_level])}>{data.current_level}</span>
            </div>
            <div className="space-y-1">
              <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Change to</label>
              <select
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
                className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono w-full"
              >
                {Object.keys(data.tiers).map((lvl) => (
                  <option key={lvl} value={lvl}>{lvl}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="space-y-1">
            <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Fields captured at <span className={levelColor[selected]}>{selected}</span></p>
            <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2.5 whitespace-pre-wrap text-[var(--color-text-muted)] max-h-24 overflow-auto">
              {(data.tiers[selected] ?? []).join(', ')}
            </pre>
          </div>

          <button
            onClick={submit}
            disabled={submitting || selected === data.current_level}
            className={clsx(
              'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
              submitting || selected === data.current_level
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {submitting ? 'validating…' : 'Set'}
          </button>

          {result && (
            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] px-3 py-2 space-y-2">
              <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Restart required</p>
              <div className="relative">
                <pre className="text-[11px] font-mono text-[var(--color-text)] pr-12">{result.instruction}</pre>
                <div className="absolute top-0 right-0">
                  <CopyButton text={result.instruction} label="cmd" />
                </div>
              </div>
              <p className="text-[10px] text-[var(--color-text-muted)]">{result.note}</p>
            </div>
          )}
        </>
      )}

      <p className="text-[10px] text-[var(--color-text-faint)]">{data?.note ?? ''}</p>
    </section>
  );
}

function lintRego(src: string): string | null {
  const s = src.trim();
  if (!s) return null;
  if (!s.includes('package ')) return 'Missing package declaration';
  const opens = (s.match(/\{/g) ?? []).length;
  const closes = (s.match(/\}/g) ?? []).length;
  if (opens !== closes) return `Unbalanced braces (${opens} opening, ${closes} closing)`;
  return null;
}

const ACTIONS = ['promote', 'replay_mutating', 'evidence_export', 'dataset_license'] as const;
const RESOURCE_KINDS = ['asset', 'capsule', 'replay'] as const;

export default function PolicyTab() {
  // Form state
  const [action, setAction] = useState<string>(ACTIONS[0]);
  const [user, setUser] = useState('');
  const [roles, setRoles] = useState('');
  const [resourceKind, setResourceKind] = useState<string>(RESOURCE_KINDS[0]);
  const [resourceRef, setResourceRef] = useState('');
  const [evalScore, setEvalScore] = useState('');
  const [unsafeSkips, setUnsafeSkips] = useState('');

  // Suggestion pools loaded from the server
  const [assetRefs, setAssetRefs] = useState<string[]>([]);
  const [runIds, setRunIds] = useState<string[]>([]);

  useEffect(() => {
    api.listAssets({ limit: 500 }).then(r => setAssetRefs(r.assets.map(a => `${a.name}@${a.version}`))).catch(() => {});
    api.listRuns({ limit: 500 }).then(r => setRunIds(r.runs.map(run => run.run_id))).catch(() => {});
  }, []);

  // Rego source linting
  const [policySource, setPolicySource] = useState('');
  const [lintError, setLintError] = useState<string | null>(null);

  useEffect(() => {
    const id = setTimeout(() => setLintError(lintRego(policySource)), 300);
    return () => clearTimeout(id);
  }, [policySource]);

  // Result state
  const [submitting, setSubmitting] = useState(false);
  const [decision, setDecision] = useState<PolicyDecision | null>(null);
  const [opaNotFound, setOpaNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [explainMode, setExplainMode] = useState(false);
  const [showTrace, setShowTrace] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setDecision(null);
    setOpaNotFound(false);
    setError(null);
    setShowTrace(false);

    const input: PolicyInput = {
      action,
      subject: {
        user: user.trim() || 'anonymous',
        roles: roles.split(',').map(r => r.trim()).filter(Boolean),
      },
      resource: {
        kind: resourceKind,
        ref: resourceRef.trim() || 'unknown',
        ...(action === 'promote' && evalScore.trim()
          ? { eval_score: parseFloat(evalScore) }
          : {}),
        ...(action === 'promote' && unsafeSkips.trim()
          ? { unsafe_skips: parseInt(unsafeSkips, 10) }
          : {}),
      },
    };

    try {
      const result = await api.checkPolicy(input, explainMode, policySource.trim() || undefined);
      // Detect OPA-not-found by the reason string (the backend returns allow=false with this specific message)
      if (!result.allow && result.reason.includes('opa binary not found')) {
        setOpaNotFound(true);
      }
      setDecision(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';
  const labelClass =
    'text-[10px] text-[var(--color-text-faint)] font-mono uppercase tracking-wider';

  return (
    <div className="space-y-4">
      <CaptureLevelPanel />

      <div className="flex items-center justify-between text-xs">
        <p className="text-[var(--color-text-muted)]">
          Test OPA/Rego policy rules interactively.
          <span className="font-mono ml-2 text-[10px] text-[var(--color-text-faint)]">
            · nova policy check
          </span>
        </p>
      </div>

      {/* Form */}
      <form
        onSubmit={handleSubmit}
        className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4"
      >
        <p className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">
          Policy input
        </p>

        {/* Row 1: Action */}
        <div className="space-y-1">
          <label className={labelClass}>Action</label>
          <select
            value={action}
            onChange={e => setAction(e.target.value)}
            className={inputClass}
          >
            {ACTIONS.map(a => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
        </div>

        {/* Row 2: Subject */}
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <label className={labelClass}>Subject — user</label>
            <input
              type="text"
              value={user}
              onChange={e => setUser(e.target.value)}
              placeholder="alice"
              className={inputClass}
            />
          </div>
          <div className="space-y-1">
            <label className={labelClass}>Subject — roles (comma-separated)</label>
            <input
              type="text"
              value={roles}
              onChange={e => setRoles(e.target.value)}
              placeholder="platform-engineer, reviewer"
              className={inputClass}
            />
          </div>
        </div>

        {/* Row 3: Resource */}
        <div className="grid grid-cols-[auto_1fr] gap-3">
          <div className="space-y-1">
            <label className={labelClass}>Resource kind</label>
            <select
              value={resourceKind}
              onChange={e => setResourceKind(e.target.value)}
              className={inputClass}
            >
              {RESOURCE_KINDS.map(k => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <label className={labelClass}>Resource ref</label>
            <SuggestInput
              value={resourceRef}
              onChange={setResourceRef}
              suggestions={resourceKind === 'asset' ? assetRefs : runIds}
              placeholder={resourceKind === 'asset' ? 'my-model@v1.2.3' : 'run_id'}
              className={inputClass}
            />
          </div>
        </div>

        {/* Conditional: promote-only fields */}
        {action === 'promote' && (
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <label className={labelClass}>Eval score (0–1, optional)</label>
              <input
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={evalScore}
                onChange={e => setEvalScore(e.target.value)}
                placeholder="0.85"
                className={inputClass}
              />
            </div>
            <div className="space-y-1">
              <label className={labelClass}>Unsafe skips (optional)</label>
              <input
                type="number"
                min={0}
                value={unsafeSkips}
                onChange={e => setUnsafeSkips(e.target.value)}
                placeholder="0"
                className={inputClass}
              />
            </div>
          </div>
        )}

        {/* Rego source */}
        <div className="space-y-1">
          <label className={labelClass}>Rego source (optional — evaluated when provided; uses bundled policy if empty)</label>
          <textarea
            value={policySource}
            onChange={e => setPolicySource(e.target.value)}
            rows={6}
            placeholder={'package novafabric.authz\n\ndefault allow = false\n\nallow {\n  input.subject.roles[_] == "platform-engineer"\n}'}
            className={`${inputClass} resize-y`}
          />
          {lintError && (
            <p className="text-[11px] text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)] rounded px-2 py-1 mt-1">
              ⚠ {lintError} — advisory only, you can still save
            </p>
          )}
        </div>

        {/* Submit */}
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={submitting}
            className={clsx(
              'text-xs font-mono px-4 py-1.5 rounded border',
              submitting
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-wait'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {submitting ? 'checking…' : 'run policy check'}
          </button>
          <label className="flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={explainMode}
              onChange={e => setExplainMode(e.target.checked)}
              className="accent-[var(--color-accent)] h-3 w-3"
            />
            <span className="text-[10px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider">explain</span>
          </label>
        </div>
      </form>

      {/* Error */}
      {error && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] p-4 text-sm text-[var(--color-status-failure)]">
          <p>Error: {error}</p>
        </div>
      )}

      {/* OPA not found warning */}
      {opaNotFound && decision && (
        <div className="rounded border border-amber-500 bg-[color-mix(in_oklab,#f59e0b_10%,transparent)] p-4 space-y-1">
          <p className="text-xs font-semibold text-amber-500 uppercase tracking-wider">OPA not installed</p>
          <p className="text-xs text-[var(--color-text-muted)]">{decision.reason}</p>
          <p className="text-xs text-[var(--color-text-faint)] font-mono mt-1">
            Install: <a
              href="https://www.openpolicyagent.org/docs/latest/#running-opa"
              target="_blank"
              rel="noopener noreferrer"
              className="underline"
            >
              openpolicyagent.org/docs/latest
            </a>
          </p>
        </div>
      )}

      {/* Decision result (only shown when OPA is available) */}
      {decision && !opaNotFound && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
          {/* ALLOW / DENY badge */}
          <div
            className={clsx(
              'px-6 py-5 flex items-center gap-4 border-b border-[var(--color-border)]',
              decision.allow
                ? 'bg-[color-mix(in_oklab,var(--color-status-success)_10%,var(--color-bg-raised))]'
                : 'bg-[color-mix(in_oklab,var(--color-status-failure)_10%,var(--color-bg-raised))]',
            )}
          >
            <span
              className={clsx(
                'text-2xl font-bold font-mono tracking-widest px-4 py-2 rounded',
                decision.allow
                  ? 'text-[var(--color-status-success)] border border-[color-mix(in_oklab,var(--color-status-success)_40%,transparent)]'
                  : 'text-[var(--color-status-failure)] border border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)]',
              )}
            >
              {decision.allow ? 'ALLOW' : 'DENY'}
            </span>
            {decision.reason && (
              <p className="text-sm text-[var(--color-text-muted)] flex-1">{decision.reason}</p>
            )}
          </div>

          {/* Metadata row */}
          <div className="px-4 py-3 flex flex-wrap items-center gap-4 text-[10px] font-mono text-[var(--color-text-faint)]">
            <span>
              <span className="text-[var(--color-text-muted)] mr-1">decision_id</span>
              {decision.decision_id || '—'}
            </span>
            <span>
              <span className="text-[var(--color-text-muted)] mr-1">policy_path</span>
              {decision.policy_path || '—'}
            </span>
            {decision.trace_text && (
              <button
                onClick={() => setShowTrace(v => !v)}
                className="ml-auto text-[var(--color-accent)] hover:underline"
              >
                {showTrace ? 'hide trace' : 'show trace ↓'}
              </button>
            )}
          </div>

          {/* OPA trace panel */}
          {showTrace && decision.trace_text && (
            <div className="border-t border-[var(--color-border)]">
              <div className="px-4 py-2 flex items-center gap-2 bg-[var(--color-bg-sunken)]">
                <span className="text-[10px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider">
                  opa trace — explain full
                </span>
              </div>
              <pre className="px-4 py-3 text-[11px] font-mono text-[var(--color-text-muted)] overflow-x-auto max-h-64 overflow-y-auto whitespace-pre leading-relaxed">
                {decision.trace_text}
              </pre>
            </div>
          )}
        </div>
      )}

      <PolicyTestPanel />
      <PolicyExplainPanel />
      <PolicyInventoryPanel />
      <PolicySignPanel />
    </div>
  );
}

function PolicyTestPanel() {
  const [bundlePath, setBundlePath] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; exit_code?: number; output: string; bundle_path?: string; backend?: string; reason?: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.policyTest(bundlePath.trim() || undefined);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [bundlePath]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Policy Test Suite</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Run OPA test suite against the bundle — <code className="font-mono">nova policy test</code>
          </p>
        </div>
      </div>
      <div className="flex gap-2">
        <input
          type="text"
          value={bundlePath}
          onChange={e => setBundlePath(e.target.value)}
          placeholder="bundle path (optional, uses default)"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <button
          onClick={run}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 transition-colors"
        >
          {loading ? '…' : 'Run tests'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <span className={clsx(
              'text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded',
              result.backend === 'stub'
                ? 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]'
                : result.ok
                  ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                  : 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]',
            )}>
              {result.backend === 'stub' ? 'opa not found' : (result.ok ? 'pass' : 'fail')}
            </span>
            {result.exit_code !== undefined && (
              <span className="text-[10px] text-[var(--color-text-faint)] font-mono">exit {result.exit_code}</span>
            )}
            {result.bundle_path && (
              <span className="text-[10px] text-[var(--color-text-faint)] font-mono truncate">{result.bundle_path}</span>
            )}
          </div>
          <pre className="text-[11px] font-mono text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-3 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap">
            {result.reason ?? result.output}
          </pre>
        </div>
      )}
    </section>
  );
}

function PolicyExplainPanel() {
  const [decisionId, setDecisionId] = useState('');
  const [recentIds, setRecentIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; decision_id: string; entries: Array<Record<string, unknown>>; count: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.policyRecentDecisions().then(r => setRecentIds(r.decision_ids)).catch(() => {});
  }, []);

  const lookup = useCallback(async () => {
    const id = decisionId.trim();
    if (!id) return;
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.policyExplain(id);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [decisionId]);

  const inputClass = 'flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Policy Explain</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          Look up a decision by ID from the audit log — <code className="font-mono">nova policy explain &lt;decision_id&gt;</code>
        </p>
      </div>
      <div className="flex gap-2">
        <SuggestInput
          value={decisionId}
          onChange={setDecisionId}
          onEnter={lookup}
          suggestions={recentIds}
          placeholder="decision_id (UUID)"
          className={inputClass}
        />
        <button
          onClick={lookup}
          disabled={loading || !decisionId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 transition-colors"
        >
          {loading ? '…' : 'Explain'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="space-y-1.5">
          <p className="text-[10px] text-[var(--color-text-faint)] font-mono">
            {result.count} audit log entr{result.count === 1 ? 'y' : 'ies'} for <span className="text-[var(--color-text)]">{result.decision_id}</span>
          </p>
          {result.entries.length === 0 ? (
            <p className="text-xs text-[var(--color-text-faint)]">No entries found for this decision ID.</p>
          ) : (
            <pre className="text-[11px] font-mono text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-3 overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap">
              {JSON.stringify(result.entries, null, 2)}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}

function fmtBytes(n: number): string {
  return n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`;
}

function PolicyInventoryPanel() {
  const [namespace, setNamespace] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; bundle_path: string; rego_files: Array<{ file: string; size_bytes: number }>; policy_db: string; signed_policies: Array<{ version: number; namespace: string; created_at: string }> } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.policyList(namespace.trim() || undefined);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [namespace]);

  const thClass = 'text-left text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] px-2 py-1';
  const tdClass = 'text-[11px] font-mono text-[var(--color-text-muted)] px-2 py-1 border-t border-[var(--color-border)]';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Policy Inventory</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          Rego bundle files and signed promotion policies — <code className="font-mono">nova policy list</code>
        </p>
      </div>
      <div className="flex gap-2">
        <input
          type="text"
          value={namespace}
          onChange={e => setNamespace(e.target.value)}
          placeholder="namespace filter (optional)"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <button
          onClick={load}
          disabled={loading}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 transition-colors"
        >
          {loading ? '…' : 'Load'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Rego files</p>
            <p className="text-[10px] text-[var(--color-text-faint)] font-mono truncate">{result.bundle_path}</p>
            {result.rego_files.length === 0 ? (
              <p className="text-xs text-[var(--color-text-faint)]">(none)</p>
            ) : (
              <table className="w-full bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
                <thead>
                  <tr>
                    <th className={thClass}>file</th>
                    <th className={thClass}>size</th>
                  </tr>
                </thead>
                <tbody>
                  {result.rego_files.map(f => (
                    <tr key={f.file}>
                      <td className={tdClass}>{f.file}</td>
                      <td className={tdClass}>{fmtBytes(f.size_bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="space-y-1.5">
            <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Signed promotion policies</p>
            <p className="text-[10px] text-[var(--color-text-faint)] font-mono truncate">{result.policy_db}</p>
            {result.signed_policies.length === 0 ? (
              <p className="text-xs text-[var(--color-text-faint)]">(none)</p>
            ) : (
              <table className="w-full bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
                <thead>
                  <tr>
                    <th className={thClass}>version</th>
                    <th className={thClass}>namespace</th>
                    <th className={thClass}>created_at</th>
                  </tr>
                </thead>
                <tbody>
                  {result.signed_policies.map(p => (
                    <tr key={`${p.namespace}-${p.version}`}>
                      <td className={tdClass}>{p.version}</td>
                      <td className={tdClass}>{p.namespace}</td>
                      <td className={tdClass}>{p.created_at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function PolicySignPanel() {
  const [keyPath, setKeyPath] = useState('');
  const [certPath, setCertPath] = useState('');
  const [proposers, setProposers] = useState('');
  const [approvers, setApprovers] = useState('');
  const [bypassHours, setBypassHours] = useState('24');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; version: number; proposers: string[]; approvers: string[]; bypass_valid_hours: number; policy_db: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const splitSubjects = (s: string) => s.split(',').map(x => x.trim()).filter(Boolean);

  const sign = useCallback(async () => {
    setSubmitting(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.policySign({
        key_path: keyPath.trim(),
        cert_path: certPath.trim(),
        proposer_subjects: splitSubjects(proposers),
        approver_subjects: splitSubjects(approvers),
        ...(bypassHours.trim() ? { bypass_valid_hours: parseInt(bypassHours, 10) } : {}),
      });
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [keyPath, certPath, proposers, approvers, bypassHours]);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';
  const labelClass =
    'text-[10px] text-[var(--color-text-faint)] font-mono uppercase tracking-wider';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Sign Promotion Policy</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          Create a versioned ECDSA-signed maker-checker policy — <code className="font-mono">nova policy sign</code>
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className={labelClass}>Key path</label>
          <input
            type="text"
            value={keyPath}
            onChange={e => setKeyPath(e.target.value)}
            placeholder="~/.novafabric/keys/policy.key"
            className={inputClass}
          />
        </div>
        <div className="space-y-1">
          <label className={labelClass}>Cert path</label>
          <input
            type="text"
            value={certPath}
            onChange={e => setCertPath(e.target.value)}
            placeholder="~/.novafabric/keys/policy.crt"
            className={inputClass}
          />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className={labelClass}>Proposer subjects (comma-separated)</label>
          <input
            type="text"
            value={proposers}
            onChange={e => setProposers(e.target.value)}
            placeholder="alice, bob"
            className={inputClass}
          />
        </div>
        <div className="space-y-1">
          <label className={labelClass}>Approver subjects (comma-separated)</label>
          <input
            type="text"
            value={approvers}
            onChange={e => setApprovers(e.target.value)}
            placeholder="carol, dave"
            className={inputClass}
          />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className={labelClass}>Bypass valid hours</label>
          <input
            type="number"
            min={1}
            max={168}
            value={bypassHours}
            onChange={e => setBypassHours(e.target.value)}
            placeholder="24"
            className={inputClass}
          />
        </div>
      </div>
      <button
        onClick={sign}
        disabled={submitting}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          submitting
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-wait'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {submitting ? 'signing…' : 'Sign & Store'}
      </button>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {result && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] px-3 py-2 space-y-1">
          <p className="text-xs text-[var(--color-status-success)] font-mono">
            Policy signed and stored: version {result.version}
          </p>
          <p className="text-[10px] text-[var(--color-text-muted)] font-mono">
            proposers: {result.proposers.join(', ')} · approvers: {result.approvers.join(', ')} · bypass {result.bypass_valid_hours}h
          </p>
          <p className="text-[10px] text-[var(--color-text-faint)] font-mono truncate">{result.policy_db}</p>
        </div>
      )}
    </section>
  );
}
