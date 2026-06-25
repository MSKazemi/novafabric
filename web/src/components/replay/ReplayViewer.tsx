import { useState } from 'react';
import { clsx } from 'clsx';
import { diffReport as fixtureDiffReport, capsules, type DiffReport } from '../../lib/fixtures';

// ---- word-level diff helpers (D-1) ----

type DiffToken = { text: string; type: 'same' | 'removed' | 'added' };

function tokenize(s: string): string[] {
  return s.split(/(\s+)/).filter(t => t.length > 0);
}

function lcs(a: string[], b: string[]): number[][] {
  const m = a.length, n = b.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1] + 1 : Math.max(dp[i-1][j], dp[i][j-1]);
  return dp;
}

function wordDiff(before: string, after: string): DiffToken[] {
  if (before.length + after.length > 4000) {
    return [{ text: before, type: 'removed' }, { text: after, type: 'added' }];
  }
  const a = tokenize(before), b = tokenize(after);
  const dp = lcs(a, b);
  const result: DiffToken[] = [];
  let i = a.length, j = b.length;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i-1] === b[j-1]) {
      result.unshift({ text: a[i-1], type: 'same' });
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
      result.unshift({ text: b[j-1], type: 'added' });
      j--;
    } else {
      result.unshift({ text: a[i-1], type: 'removed' });
      i--;
    }
  }
  return result;
}

function WordDiffView({ tokens, side }: { tokens: DiffToken[]; side: 'before' | 'after' }) {
  return (
    <span className="leading-relaxed break-words">
      {tokens.map((t, i) => {
        if (t.type === 'same') return <span key={i}>{t.text}</span>;
        if (side === 'before' && t.type === 'removed')
          return <mark key={i} className="rounded bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] text-[var(--color-status-failure)] line-through">{t.text}</mark>;
        if (side === 'after' && t.type === 'added')
          return <mark key={i} className="rounded bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)] text-[var(--color-status-success)]">{t.text}</mark>;
        return null;
      })}
    </span>
  );
}

type Mode = 'forensic' | 'mocked' | 'semantic' | 'exact';

interface ModeMeta {
  value: Mode;
  label: string;
  available: boolean;
  guarantee: string;
  oneLiner: string;
  bannerTone: 'success' | 'pending' | 'failure' | 'muted';
}

const MODES: ModeMeta[] = [
  {
    value: 'forensic',
    label: 'Forensic',
    available: true,
    guarantee: 'No re-execution. Read-only inspection of what the capsule recorded.',
    oneLiner: 'The audit mode.',
    bannerTone: 'success',
  },
  {
    value: 'mocked',
    label: 'Mocked',
    available: true,
    guarantee: 'Re-execute the agent code. Serve every model and tool call from the capsule cache.',
    oneLiner: 'Same control flow, same outputs, no network.',
    bannerTone: 'success',
  },
  {
    value: 'semantic',
    label: 'Semantic',
    available: true,
    guarantee: 'Re-execute, then a judge model compares meaning. Requires a judge model in real use.',
    oneLiner: 'Useful for "did the meaning change?"',
    bannerTone: 'pending',
  },
  {
    value: 'exact',
    label: 'Exact',
    available: false,
    guarantee: 'Re-execute end-to-end with byte-identical results.',
    oneLiner: 'Available only for local models or fully containerized environments. Greyed out for remote LLMs.',
    bannerTone: 'muted',
  },
];

export default function ReplayViewer() {
  const [mode, setMode] = useState<Mode>('mocked');
  const meta = MODES.find((m) => m.value === mode)!;

  return (
    <div>
      {/* Mode tab strip */}
      <div role="tablist" aria-label="Replay modes" className="inline-flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-1">
        {MODES.map((m) => (
          <button
            key={m.value}
            role="tab"
            aria-selected={mode === m.value}
            onClick={() => m.available && setMode(m.value)}
            disabled={!m.available}
            title={!m.available ? 'Disabled for remote LLM runs' : m.guarantee}
            className={clsx(
              'px-4 py-2 text-sm rounded-md transition-colors',
              mode === m.value
                ? 'bg-[var(--color-bg-raised)] text-[var(--color-text)]'
                : m.available
                ? 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)]'
                : 'text-[var(--color-text-faint)] cursor-not-allowed line-through',
            )}
          >
            {m.label}
          </button>
        ))}
      </div>

      {/* Mode banner */}
      <div className={clsx(
        'mt-4 rounded-lg border p-4',
        meta.bannerTone === 'success' && 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]',
        meta.bannerTone === 'pending' && 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)]',
        meta.bannerTone === 'muted' && 'border-[var(--color-border)] bg-[var(--color-bg-sunken)]',
      )}>
        <p className={clsx(
          'text-sm font-medium',
          meta.bannerTone === 'success' && 'text-[var(--color-status-success)]',
          meta.bannerTone === 'pending' && 'text-[var(--color-status-pending)]',
          meta.bannerTone === 'muted' && 'text-[var(--color-text-muted)]',
        )}>
          {meta.guarantee}
        </p>
        <p className="mt-1 text-sm text-[var(--color-text-muted)]">{meta.oneLiner}</p>
      </div>

      {/* Per-mode pane */}
      {mode === 'forensic' && <ForensicPane />}
      {mode === 'mocked' && <MockedPane />}
      {mode === 'semantic' && <SemanticPane />}
      {mode === 'exact' && <ExactPane />}

      {/* Structural diff (always visible below) */}
      <div className="mt-12">
        <DiffSection />
      </div>
    </div>
  );
}

function ForensicPane() {
  return (
    <div className="mt-6 grid md:grid-cols-2 gap-4 text-sm">
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h3 className="text-[var(--color-text)] font-medium mb-2">RUN_A — captured outputs</h3>
        <pre className="whitespace-pre-wrap text-xs font-mono text-[var(--color-text-muted)] leading-relaxed">{`Found two issues. Line 42:
  off-by-one in the slice.
Line 78: missing null-check
  on the optional param.
Style: prefer \`is None\`
  over \`== None\`.`}</pre>
      </div>
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h3 className="text-[var(--color-text)] font-medium mb-2">Forensic summary</h3>
        <ul className="text-xs space-y-1.5 text-[var(--color-text-muted)] font-mono">
          <li>capsule_status: <span className="text-[var(--color-status-success)]">success</span></li>
          <li>exit_code: 0</li>
          <li>duration_ms: 4127</li>
          <li>model_calls: 2 recorded</li>
          <li>tool_calls: 2 recorded</li>
          <li>secret_findings: 0</li>
          <li>chain_hash_verified: <span className="text-[var(--color-status-success)]">yes</span></li>
        </ul>
      </div>
    </div>
  );
}

function MockedPane() {
  return (
    <div className="mt-6 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-[var(--color-text)] font-medium">Mocked replay — RUN_A</h3>
        <code className="text-xs text-[var(--color-text-faint)]">replay_id: 01KT2P4Y8WGTHE9MRP4ZRX7QP3</code>
      </div>
      <ul className="space-y-2 text-sm text-[var(--color-text-muted)]">
        <li className="flex items-center gap-2">
          <span className="text-[var(--color-status-success)]">✓</span>
          <span><code className="text-[var(--color-text)]">openai.completion</code> · served from cache (1240ms recorded → 0ms replay)</span>
        </li>
        <li className="flex items-center gap-2">
          <span className="text-[var(--color-status-success)]">✓</span>
          <span><code className="text-[var(--color-text)]">openai.completion</code> · served from cache (980ms recorded → 0ms replay)</span>
        </li>
        <li className="flex items-center gap-2">
          <span className="text-[var(--color-status-success)]">✓</span>
          <span><code className="text-[var(--color-text)]">mcp.read-file</code> · served from cache</span>
        </li>
        <li className="flex items-center gap-2">
          <span className="text-[var(--color-status-success)]">✓</span>
          <span><code className="text-[var(--color-text)]">mcp.run-tests</code> · served from cache</span>
        </li>
      </ul>
      <div className="mt-4 pt-4 border-t border-[var(--color-border)] grid grid-cols-3 gap-2 text-xs text-[var(--color-text-muted)]">
        <div><span className="text-[var(--color-text)] font-mono text-base block">2/2</span>model calls mocked</div>
        <div><span className="text-[var(--color-text)] font-mono text-base block">2/2</span>tool calls mocked</div>
        <div><span className="text-[var(--color-text)] font-mono text-base block">0</span>env warnings</div>
      </div>
    </div>
  );
}

function SemanticPane() {
  return (
    <div className="mt-6 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5">
      <h3 className="text-[var(--color-text)] font-medium mb-3">Semantic comparison — RUN_A vs RUN_B</h3>
      <div className="grid md:grid-cols-2 gap-3 text-xs">
        <div className="rounded border border-[var(--color-border)] p-3 bg-[var(--color-bg-sunken)]">
          <p className="text-[var(--color-text-muted)] uppercase tracking-wider text-[10px] mb-1">RUN_A output (v0.1.0 prompt)</p>
          <p className="text-[var(--color-text)] font-mono leading-relaxed">Found two issues. Line 42: off-by-one in the slice. Line 78: missing null-check on the optional param. Style: prefer `is None` over `== None`.</p>
        </div>
        <div className="rounded border border-[var(--color-border)] p-3 bg-[var(--color-bg-sunken)]">
          <p className="text-[var(--color-text-muted)] uppercase tracking-wider text-[10px] mb-1">RUN_B output (v0.2.0 prompt)</p>
          <p className="text-[var(--color-text)] font-mono leading-relaxed">LGTM. Minor style: prefer `is None`.</p>
        </div>
      </div>
      <div className="mt-4 pt-3 border-t border-[var(--color-border)] text-sm">
        <p className="text-[var(--color-text-muted)]">
          <span className="text-[var(--color-status-pending)]">⚠ Judge model would assess:</span> RUN_B fails to flag the
          off-by-one and the missing null-check. Semantic regression detected.
          <span className="block mt-1 text-xs text-[var(--color-text-faint)]">In real use this comparison runs against a configured judge model. The showcase visualizes the result; it does not run a live judge.</span>
        </p>
      </div>
    </div>
  );
}

function ExactPane() {
  return (
    <div className="mt-6 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-5">
      <h3 className="text-[var(--color-text)] font-medium mb-2">Why exact mode is greyed out</h3>
      <p className="text-sm text-[var(--color-text-muted)] leading-relaxed">
        Exact replay requires byte-identical re-execution. Remote LLM APIs do not
        guarantee determinism — <code className="text-[var(--color-text)]">temperature=0</code>
        is helpful but not sufficient (provider-side updates, sampling implementations,
        load-shed routing all break it). Exact mode is reserved for runs where every
        model call hit a local model or a pinned containerized endpoint.
      </p>
      <p className="mt-3 text-xs text-[var(--color-text-faint)]">
        Many tools quietly call this "replay" anyway. We don't.
      </p>
    </div>
  );
}

export function DiffSection({ report }: { report?: DiffReport } = {}) {
  const d = report ?? fixtureDiffReport;
  return (
    <div>
      <header className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-2xl font-medium tracking-tight text-[var(--color-text)]">Structural diff</h2>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            <code className="text-[var(--color-text)] font-mono text-xs">{d.run_a_id.slice(0, 10)}…</code> vs
            <code className="text-[var(--color-text)] font-mono text-xs ml-1">{d.run_b_id.slice(0, 10)}…</code>
          </p>
        </div>
        <div className="flex gap-3 text-xs">
          <DiffSummaryStat label="changed" count={d.summary.changed} tone="failure" />
          <DiffSummaryStat label="added" count={d.summary.added} tone="success" />
          <DiffSummaryStat label="removed" count={d.summary.removed} tone="muted" />
        </div>
      </header>

      <div className="space-y-3">
        <DiffPanel title="Environment" change_count={d.sections.environment.changes.length}>
          {d.sections.environment.changes.length === 0 ? (
            <p className="text-sm text-[var(--color-text-muted)]">No environment changes.</p>
          ) : (
            <ul className="space-y-2">
              {d.sections.environment.changes.map((c, i) => (
                <li key={i} className="text-xs">
                  <div className="flex items-center gap-2 mb-1">
                    <code className="text-[var(--color-text-muted)]">{c.field}</code>
                    <span className={clsx(
                      'px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider',
                      c.severity === 'major' && 'bg-[color-mix(in_oklab,var(--color-status-failure)_15%,transparent)] text-[var(--color-status-failure)]',
                      c.severity === 'minor' && 'bg-[color-mix(in_oklab,var(--color-status-pending)_15%,transparent)] text-[var(--color-status-pending)]',
                    )}>{c.severity}</span>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2 font-mono">
                    <div className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)] p-2 text-[var(--color-text-muted)] text-xs leading-relaxed break-all">
                      <span className="text-[var(--color-status-failure)] mr-1">−</span>
                      {String(c.before ?? '')}
                    </div>
                    <div className="rounded border border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)] p-2 text-[var(--color-text-muted)] text-xs leading-relaxed break-all">
                      <span className="text-[var(--color-status-success)] mr-1">+</span>
                      {String(c.after ?? '')}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </DiffPanel>

        <DiffPanel title="Model calls" change_count={d.sections.model_calls.changed} defaultOpen>
          <p className="text-xs text-[var(--color-text-muted)] mb-3">
            {d.sections.model_calls.aligned} aligned / {d.sections.model_calls.changed} changed / {d.sections.model_calls.added} added / {d.sections.model_calls.removed} removed
          </p>
          <ul className="space-y-3">
            {d.sections.model_calls.pairs.map((pair) => (
              <li key={pair.index} className="rounded border border-[var(--color-border)] p-3 bg-[var(--color-bg-raised)]">
                <header className="flex items-center justify-between text-xs mb-2">
                  <code className="text-[var(--color-text)] font-mono">model_call[{pair.index}]</code>
                  <code className="text-[var(--color-text-faint)]">{pair.a_call_id} → {pair.b_call_id}</code>
                </header>
                {pair.changes.length === 0 ? (
                  <p className="text-xs text-[var(--color-status-success)]">no changes</p>
                ) : (
                  <ul className="space-y-2">
                    {pair.changes.map((c, i) => {
                      const isTextEligible = typeof c.before === 'string' && typeof c.after === 'string' && c.before.length + c.after.length > 20;
                      const tokens = isTextEligible ? wordDiff(c.before, c.after) : null;
                      return (
                        <li key={i} className="text-xs">
                          <div className="flex items-center gap-2 mb-1">
                            <code className="text-[var(--color-text-muted)]">{c.field}</code>
                            <span className={clsx(
                              'px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider',
                              c.severity === 'major' && 'bg-[color-mix(in_oklab,var(--color-status-failure)_15%,transparent)] text-[var(--color-status-failure)]',
                              c.severity === 'minor' && 'bg-[color-mix(in_oklab,var(--color-status-pending)_15%,transparent)] text-[var(--color-status-pending)]',
                            )}>{c.severity}</span>
                          </div>
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-2 mt-2 font-mono">
                            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)] p-2 text-[var(--color-text-muted)] text-xs leading-relaxed">
                              <span className="text-[var(--color-status-failure)] mr-1">−</span>
                              {tokens ? <WordDiffView tokens={tokens} side="before" /> : c.before}
                            </div>
                            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)] p-2 text-[var(--color-text-muted)] text-xs leading-relaxed">
                              <span className="text-[var(--color-status-success)] mr-1">+</span>
                              {tokens ? <WordDiffView tokens={tokens} side="after" /> : c.after}
                            </div>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </DiffPanel>

        <DiffPanel title="Tool calls" change_count={(d.sections.tool_calls.changed ?? 0) + (d.sections.tool_calls.added ?? 0) + (d.sections.tool_calls.removed ?? 0)}>
          {d.sections.tool_calls.pairs.length === 0 ? (
            <p className="text-sm text-[var(--color-text-muted)]">No tool calls recorded.</p>
          ) : (
            <>
              <p className="text-xs text-[var(--color-text-muted)] mb-3">
                {d.sections.tool_calls.aligned} aligned / {d.sections.tool_calls.changed ?? 0} changed / {d.sections.tool_calls.added ?? 0} added / {d.sections.tool_calls.removed ?? 0} removed
              </p>
              <ul className="space-y-1.5">
                {d.sections.tool_calls.pairs.map((p, i) => {
                  const tone = p.added ? 'success' : p.removed ? 'failure' : p.result_changed ? 'pending' : 'muted';
                  const toneClasses: Record<string, string> = {
                    success: 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)]',
                    failure: 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_5%,transparent)]',
                    pending: 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_5%,transparent)]',
                    muted: 'border-[var(--color-border)] bg-transparent',
                  };
                  const badge = p.added ? '+ added' : p.removed ? '− removed' : p.result_changed ? '~ result changed' : '= unchanged';
                  const badgeClasses: Record<string, string> = {
                    success: 'text-[var(--color-status-success)]',
                    failure: 'text-[var(--color-status-failure)]',
                    pending: 'text-[var(--color-status-pending)]',
                    muted: 'text-[var(--color-text-faint)]',
                  };
                  return (
                    <li key={i} className={clsx('rounded border px-3 py-2 flex items-center gap-3 text-xs font-mono', toneClasses[tone])}>
                      <code className="text-[var(--color-text)] flex-1 truncate">{p.tool_name ?? '(unknown)'}</code>
                      <span className={clsx('shrink-0 text-[10px]', badgeClasses[tone])}>{badge}</span>
                      {p.mutation_class && (
                        <span className={clsx(
                          'px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider shrink-0',
                          p.mutation_class === 'read' && 'bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)]',
                          p.mutation_class === 'write' && 'bg-[color-mix(in_oklab,var(--color-status-pending)_15%,transparent)] text-[var(--color-status-pending)]',
                          p.mutation_class === 'delete' && 'bg-[color-mix(in_oklab,var(--color-status-failure)_15%,transparent)] text-[var(--color-status-failure)]',
                          p.mutation_class === 'external_side_effect' && 'bg-[color-mix(in_oklab,var(--color-status-pending)_20%,transparent)] text-[var(--color-status-pending)]',
                          !['read','write','delete','external_side_effect'].includes(p.mutation_class) && 'bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)]',
                        )}>
                          {p.mutation_class === 'external_side_effect' ? 'ext. effect' : p.mutation_class}
                        </span>
                      )}
                      {p.tool_call_id_a && <code className="text-[var(--color-text-faint)] text-[10px] shrink-0 hidden sm:block">{p.tool_call_id_a.slice(0, 10)}</code>}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </DiffPanel>

        <DiffPanel title="Outputs" change_count={d.sections.outputs.changes.length} defaultOpen>
          <ul className="space-y-2">
            {d.sections.outputs.changes.map((c, i) => (
              <li key={i} className="text-xs">
                <code className="text-[var(--color-text)] font-mono">{c.path}</code>
                <div className="mt-1 grid grid-cols-2 gap-2 font-mono text-[10px] text-[var(--color-text-faint)]">
                  <span>before: {c.before_hash?.slice(0, 16)}…</span>
                  <span>after:  {c.after_hash?.slice(0, 16)}…</span>
                </div>
              </li>
            ))}
          </ul>
        </DiffPanel>
      </div>
    </div>
  );
}

function DiffSummaryStat({ label, count, tone }: { label: string; count: number; tone: 'success' | 'failure' | 'muted' }) {
  const toneClass =
    tone === 'success' ? 'text-[var(--color-status-success)]'
    : tone === 'failure' ? 'text-[var(--color-status-failure)]'
    : 'text-[var(--color-text-muted)]';
  return (
    <div className="flex items-center gap-1.5">
      <span className={clsx('font-mono text-base', toneClass)}>{count}</span>
      <span className="text-[var(--color-text-faint)] uppercase tracking-wider text-[10px]">{label}</span>
    </div>
  );
}

function DiffPanel({ title, change_count, defaultOpen, children }: { title: string; change_count: number; defaultOpen?: boolean; children: React.ReactNode }) {
  return (
    <details open={defaultOpen} className="group rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)]">
      <summary className="flex items-center justify-between cursor-pointer list-none px-4 py-3 hover:bg-[var(--color-bg-sunken)] rounded-lg transition-colors">
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-[var(--color-text)]">{title}</span>
          <span className={clsx(
            'px-1.5 py-0.5 rounded text-[10px] font-medium',
            change_count === 0
              ? 'bg-[var(--color-bg-sunken)] text-[var(--color-text-muted)]'
              : 'bg-[color-mix(in_oklab,var(--color-status-failure)_15%,transparent)] text-[var(--color-status-failure)]',
          )}>
            {change_count} {change_count === 1 ? 'change' : 'changes'}
          </span>
        </div>
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="text-[var(--color-text-faint)] transition-transform group-open:rotate-90" aria-hidden="true">
          <path d="M5 3L9 7L5 11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </summary>
      <div className="px-4 pb-4 pt-1 border-t border-[var(--color-border)]">
        {children}
      </div>
    </details>
  );
}
