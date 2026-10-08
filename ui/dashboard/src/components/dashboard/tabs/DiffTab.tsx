import { useState, useEffect, useCallback, useRef } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import type { RunSummary, FullCapsule } from '../../../lib/api';
import type { DiffReport } from '../../../lib/fixtures';
import { DiffSection } from '../../replay/ReplayViewer';
import TraceDiffGraph from '../../diff/TraceDiffGraph';
import { ErrorBox } from '../helpers';
import { runLabel, shortTs } from '../../../lib/runUtils';
import { SuggestInput } from '../../ui/SuggestInput';

function ExitDot({ exitCode }: { exitCode: number | null }) {
  const ok = exitCode === 0 || exitCode === null;
  return <span className={clsx('inline-block w-2 h-2 rounded-full shrink-0', ok ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-failure)]')} />;
}

function suggestPairs(runs: RunSummary[]): Array<[RunSummary, RunSummary]> {
  const pairs: Array<[RunSummary, RunSummary]> = [];
  const used = new Set<string>();
  const groups = new Map<string, RunSummary[]>();
  for (const r of runs) {
    const pyArg = [...r.command].reverse().find(a => a.endsWith('.py')) ?? '';
    const parts = pyArg.split('/');
    const key = parts.length >= 2 ? parts[parts.length - 2] : pyArg;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(r);
  }
  for (const [, group] of groups) {
    if (group.length >= 2) {
      const [x, y] = group;
      if (!used.has(x.run_id) && !used.has(y.run_id)) {
        pairs.push([x, y]);
        used.add(x.run_id);
        used.add(y.run_id);
      }
    }
    if (pairs.length >= 3) break;
  }
  if (pairs.length === 0 && runs.length >= 2) pairs.push([runs[0], runs[1]]);
  return pairs;
}

function adaptDiffReport(raw: unknown): DiffReport {
  const wrapper = (raw as { report?: Record<string, unknown> }) ?? {};
  const r = (wrapper.report ?? raw) as Record<string, unknown>;
  const envChanges = (r.env_changes as Array<{ field: string; before: unknown; after: unknown; severity?: string }>) ?? [];
  const modelPairs = (r.model_call_pairs as Array<{ index?: number; a_call_id?: string; b_call_id?: string; changes?: Array<{ field: string; before: string; after: string; severity?: string }> }>) ?? [];
  type RawToolPair = { changed?: boolean; added?: boolean; removed?: boolean; result_changed?: boolean; tool_name?: string; tool_call_id_a?: string; tool_call_id_b?: string };
  const toolPairs = (r.tool_call_pairs as RawToolPair[]) ?? [];
  const outChanges = (r.output_changes as Array<{ path: string; before_hash: string | null; after_hash: string | null; severity?: string }>) ?? [];
  const modelChanged = modelPairs.filter(p => (p.changes?.length ?? 0) > 0).length;
  const toolChanged = toolPairs.filter(p => p.changed || p.added || p.removed).length;
  const toolAdded = toolPairs.filter(p => p.added).length;
  const toolRemoved = toolPairs.filter(p => p.removed).length;
  const summaryChanged = envChanges.length + modelChanged + toolChanged + outChanges.length;
  const summaryAdded = toolAdded;
  const summaryRemoved = toolRemoved;
  return {
    schema_version: '0.1.0',
    run_a_id: String(r.run_a_id ?? ''),
    run_b_id: String(r.run_b_id ?? ''),
    summary: { changed: summaryChanged, added: summaryAdded, removed: summaryRemoved },
    sections: {
      environment: { changes: envChanges.map(c => ({ field: c.field, before: c.before, after: c.after, severity: c.severity ?? 'minor' })) },
      model_calls: {
        aligned: modelPairs.length,
        changed: modelChanged,
        added: 0,
        removed: 0,
        pairs: modelPairs.map((p, i) => ({
          index: p.index ?? i,
          a_call_id: p.a_call_id ?? '',
          b_call_id: p.b_call_id ?? '',
          changes: (p.changes ?? []).map(c => ({ ...c, severity: c.severity ?? 'minor' })),
        })),
      },
      tool_calls: {
        aligned: toolPairs.filter(p => !p.added && !p.removed).length,
        changed: toolChanged,
        added: toolAdded,
        removed: toolRemoved,
        pairs: toolPairs,
      },
      outputs: { changes: outChanges },
    },
  };
}

const DIFF_SESSION_KEY = 'nova_diff_last';

interface MultiDiffResult {
  runId: string;
  report: DiffReport | null;
  error: string | null;
  busy: boolean;
  expanded: boolean;
  capsule: FullCapsule | null;
}

// ---- component ----

export default function DiffTab({ initialIds }: { initialIds?: string[] } = {}) {
  const [a, setA] = useState('');
  const [b, setB] = useState('');
  const [report, setReport] = useState<DiffReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [capsuleA, setCapsuleA] = useState<FullCapsule | null>(null);
  const [capsuleB, setCapsuleB] = useState<FullCapsule | null>(null);
  const [traceExpanded, setTraceExpanded] = useState(false);
  const [runSearch, setRunSearch] = useState('');
  const [autoRun, setAutoRun] = useState(false);
  const didAutoRunRef = useRef(false);
  const [multiResults, setMultiResults] = useState<MultiDiffResult[] | null>(null);

  useEffect(() => {
    if (initialIds?.length) return;
    try {
      const raw = sessionStorage.getItem(DIFF_SESSION_KEY);
      if (!raw) return;
      const stored = JSON.parse(raw) as { fromId: string; toId: string; result: DiffReport; capsuleA: FullCapsule | null; capsuleB: FullCapsule | null };
      if (stored.fromId) setA(stored.fromId);
      if (stored.toId) setB(stored.toId);
      if (stored.result) setReport(stored.result);
      if (stored.capsuleA) setCapsuleA(stored.capsuleA);
      if (stored.capsuleB) setCapsuleB(stored.capsuleB);
    } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runDiff = useCallback(async (runA: string, runB: string) => {
    setError(null); setBusy(true); setReport(null);
    setCapsuleA(null); setCapsuleB(null); setTraceExpanded(false);
    try {
      const [r, ca, cb] = await Promise.all([
        api.diff(runA, runB),
        api.getRun(runA).catch(() => null),
        api.getRun(runB).catch(() => null),
      ]);
      const adapted = adaptDiffReport(r);
      setReport(adapted);
      if (ca) setCapsuleA(ca);
      if (cb) setCapsuleB(cb);
      try {
        sessionStorage.setItem(DIFF_SESSION_KEY, JSON.stringify({ fromId: runA, toId: runB, result: adapted, capsuleA: ca ?? null, capsuleB: cb ?? null }));
      } catch { /* ignore */ }
    } catch (e) {
      setError((e as Error).message);
    } finally { setBusy(false); }
  }, []);

  const runMultiDiff = useCallback(async (baseline: string, comparisons: string[]) => {
    setMultiResults(comparisons.map(id => ({
      runId: id, report: null, error: null, busy: true, expanded: true, capsule: null,
    })));
    const settled = await Promise.allSettled(
      comparisons.map(async (cId) => {
        const [r, cap] = await Promise.all([
          api.diff(baseline, cId),
          api.getRun(cId).catch(() => null),
        ]);
        return { report: adaptDiffReport(r), capsule: cap };
      })
    );
    setMultiResults(prev => {
      if (!prev) return null;
      return prev.map((item, i) => {
        const res = settled[i];
        if (res.status === 'fulfilled') {
          return { ...item, busy: false, report: res.value.report, capsule: res.value.capsule };
        }
        return { ...item, busy: false, error: (res.reason as Error).message };
      });
    });
  }, []);

  useEffect(() => {
    setMultiResults(null);
    api.listRuns().then(r => {
      const sorted = [...r.runs].sort((x, y) => (y.created_at ?? '').localeCompare(x.created_at ?? ''));
      setRuns(sorted);
      if (initialIds && initialIds.length >= 2) {
        const [first, ...rest] = initialIds;
        setA(first);
        if (rest.length === 1) {
          setB(rest[0]);
          setAutoRun(true);
        } else {
          runMultiDiff(first, rest);
        }
      } else {
        setA(prev => {
          if (prev) return prev;
          const pairs = suggestPairs(sorted);
          if (pairs.length > 0) setTimeout(() => setB(pairs[0][1].run_id), 0);
          return pairs.length > 0 ? pairs[0][0].run_id : (sorted[0]?.run_id ?? '');
        });
      }
    }).catch(() => {});
  // Re-run when the set of IDs changes (join gives a stable string key)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialIds?.join(','), runMultiDiff]);

  // Auto-fire the diff once both values are settled and autoRun is armed
  useEffect(() => {
    if (autoRun && a && b && !busy && !didAutoRunRef.current) {
      didAutoRunRef.current = true;
      setAutoRun(false);
      runDiff(a, b);
    }
  }, [autoRun, a, b, busy, runDiff]);

  const onQuery = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    runDiff(a, b);
  }, [a, b, runDiff]);

  const pairs = runs ? suggestPairs(runs) : [];
  const recent = runs
    ? runs.filter(r => {
        const q = runSearch.trim().toLowerCase();
        if (!q) return true;
        return r.run_id.toLowerCase().includes(q) || runLabel(r).toLowerCase().includes(q);
      }).slice(0, 20)
    : [];

  return (
    <div className="space-y-4">
      {/* N-run multi-diff view — shown when 3+ runs are pushed in from RunsTab */}
      {multiResults && (
        <div className="space-y-3">
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 flex items-center justify-between gap-4 flex-wrap">
            <div>
              <p className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider mb-0.5">
                N-run comparison — {multiResults.length + 1} runs
              </p>
              <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
                Baseline: <code className="text-[var(--color-accent)]">{a.slice(0, 16)}…</code>
                {' · '}{multiResults.length} run{multiResults.length !== 1 ? 's' : ''} compared against it
              </p>
            </div>
            <button
              type="button"
              onClick={() => setMultiResults(null)}
              className="shrink-0 text-[10px] font-mono text-[var(--color-text-faint)] hover:text-[var(--color-text)] border border-[var(--color-border)] rounded px-2 py-1"
            >
              ← 2-run mode
            </button>
          </div>
          {multiResults.map((m, idx) => (
            <MultiDiffCard
              key={m.runId}
              baseline={a}
              result={m}
              onToggle={() => setMultiResults(prev => prev?.map((x, i) => i === idx ? { ...x, expanded: !x.expanded } : x) ?? null)}
            />
          ))}
        </div>
      )}

      <form onSubmit={onQuery} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 grid sm:grid-cols-[1fr_1fr_auto] gap-3">
        <SuggestInput
          value={a}
          onChange={v => { setA(v); setReport(null); sessionStorage.removeItem(DIFF_SESSION_KEY); }}
          suggestions={runs?.map(r => r.run_id) ?? []}
          placeholder="run_a"
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-sm focus:border-[var(--color-accent)] focus:outline-none"
          required
        />
        <SuggestInput
          value={b}
          onChange={v => { setB(v); setReport(null); sessionStorage.removeItem(DIFF_SESSION_KEY); }}
          suggestions={runs?.map(r => r.run_id) ?? []}
          placeholder="run_b"
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-sm focus:border-[var(--color-accent)] focus:outline-none"
          required
        />
        <button type="submit" disabled={busy || !a || !b} className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60">
          {busy ? 'Diffing…' : 'Diff'}
        </button>
      </form>

      {pairs.length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
          <p className="text-[11px] font-medium text-[var(--color-text-muted)] mb-2 uppercase tracking-wide">Suggested pairs</p>
          <div className="space-y-1.5">
            {pairs.map(([ra, rb], i) => (
              <button key={i} type="button"
                onClick={() => { setA(ra.run_id); setB(rb.run_id); setReport(null); setError(null); sessionStorage.removeItem(DIFF_SESSION_KEY); }}
                className={clsx(
                  'w-full text-left flex items-center gap-2 px-3 py-2 rounded border text-xs font-mono transition-colors',
                  a === ra.run_id && b === rb.run_id
                    ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]'
                    : 'border-[var(--color-border)] hover:bg-[var(--color-bg-sunken)]',
                )}>
                <ExitDot exitCode={ra.exit_code} />
                <span className="flex-1 truncate text-[var(--color-text)]">{runLabel(ra)}</span>
                <span className="text-[var(--color-text-faint)] shrink-0">vs</span>
                <ExitDot exitCode={rb.exit_code} />
                <span className="flex-1 truncate text-[var(--color-text)]">{runLabel(rb)}</span>
                <span className="text-[var(--color-text-faint)] shrink-0 text-[10px]">{shortTs(ra.created_at)}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {(runs ?? []).length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
          <div className="flex items-center gap-2 mb-2">
            <p className="text-[11px] font-medium text-[var(--color-text-muted)] uppercase tracking-wide shrink-0">Recent runs</p>
            <input
              type="search"
              value={runSearch}
              onChange={e => setRunSearch(e.target.value)}
              placeholder="Filter runs…"
              className="flex-1 text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1 font-mono focus:border-[var(--color-accent)] focus:outline-none"
            />
          </div>
          <div className="max-h-52 overflow-y-auto space-y-0.5 pr-1">
            {recent.map(r => (
              <div key={r.run_id} className="flex items-center gap-2 px-2 py-1.5 rounded text-xs font-mono hover:bg-[var(--color-bg-sunken)]">
                <ExitDot exitCode={r.exit_code} />
                <span className="flex-1 truncate text-[var(--color-text-muted)]" title={r.run_id}>{runLabel(r)}</span>
                <span className="text-[var(--color-text-faint)] text-[10px] shrink-0">{shortTs(r.created_at)}</span>
                <button type="button" onClick={() => { setA(r.run_id); setReport(null); sessionStorage.removeItem(DIFF_SESSION_KEY); }}
                  className={clsx('px-1.5 py-0.5 rounded text-[10px] border transition-colors shrink-0', a === r.run_id ? 'border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)]' : 'border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]')}>A</button>
                <button type="button" onClick={() => { setB(r.run_id); setReport(null); sessionStorage.removeItem(DIFF_SESSION_KEY); }}
                  className={clsx('px-1.5 py-0.5 rounded text-[10px] border transition-colors shrink-0', b === r.run_id ? 'border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)]' : 'border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]')}>B</button>
              </div>
            ))}
          </div>
        </div>
      )}

      {error && <ErrorBox message={error} />}
      {report && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5">
          <DiffSection report={report} />
        </div>
      )}

      {/* Trace diff graph — appears after a successful diff */}
      {capsuleA && capsuleB && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
          <button
            onClick={() => setTraceExpanded(e => !e)}
            className="w-full flex items-center justify-between px-5 py-3 text-sm hover:bg-[var(--color-bg-sunken)] transition-colors"
          >
            <span className="font-medium text-[var(--color-text)]">Trace comparison</span>
            <span className="text-xs text-[var(--color-text-faint)]">{traceExpanded ? '▲ collapse' : '▼ expand'}</span>
          </button>
          {traceExpanded && (
            <div className="px-5 pb-5 border-t border-[var(--color-border)]">
              <div className="pt-4">
                <TraceDiffGraph
                  runAId={a}
                  runBId={b}
                  traceA={capsuleA.trace}
                  traceB={capsuleB.trace}
                  modelCallsA={capsuleA.model_calls}
                  modelCallsB={capsuleB.model_calls}
                />
              </div>
            </div>
          )}
        </div>
      )}

      <p className="text-[10px] text-[var(--color-text-faint)] font-mono">
        CLI equivalent: <code>nova diff &lt;run-a&gt; &lt;run-b&gt;</code>
      </p>
    </div>
  );
}

function MultiDiffCard({
  baseline,
  result,
  onToggle,
}: {
  baseline: string;
  result: MultiDiffResult;
  onToggle: () => void;
}) {
  const total = result.report
    ? result.report.summary.changed + result.report.summary.added + result.report.summary.removed
    : null;
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center justify-between px-4 py-3 text-sm hover:bg-[var(--color-bg-sunken)] transition-colors"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[10px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider shrink-0">vs</span>
          <code className="font-mono text-xs text-[var(--color-text)] truncate">{result.runId.slice(0, 16)}…</code>
          {result.busy && (
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">diffing…</span>
          )}
          {result.error && (
            <span className="text-[10px] text-[var(--color-status-failure)]">error</span>
          )}
          {total !== null && (
            <span className={clsx(
              'shrink-0 text-[10px] font-mono px-1.5 py-px rounded border',
              total === 0
                ? 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]'
                : 'text-[var(--color-status-pending)] border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]',
            )}>
              {total === 0 ? 'identical' : `${total} Δ`}
            </span>
          )}
        </div>
        <span className="text-xs text-[var(--color-text-faint)] shrink-0 ml-2">
          {result.expanded ? '▲' : '▼'}
        </span>
      </button>
      {result.expanded && (
        <div className="border-t border-[var(--color-border)] p-4 space-y-1">
          {result.busy && (
            <p className="text-xs font-mono text-[var(--color-text-faint)]">
              Comparing {baseline.slice(0, 8)}… → {result.runId.slice(0, 8)}…
            </p>
          )}
          {result.error && (
            <p className="text-sm text-[var(--color-status-failure)]">Error: {result.error}</p>
          )}
          {result.report && <DiffSection report={result.report} />}
        </div>
      )}
    </div>
  );
}
