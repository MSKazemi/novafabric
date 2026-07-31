/**
 * Result panes for the three replay inspection modes: forensic/dry-run,
 * semantic similarity, and exact-eligibility.
 * Extracted verbatim from the former RunsTab monolith — behavior frozen.
 */
import { clsx } from 'clsx';
import type { ReplayResult } from './types';

export function ForensicResultPane({ result }: { result: ReplayResult }) {
  const isDryRun = result.status === 'dry_run';
  const ok = result.status === 'success' || isDryRun;
  const blockedCount = isDryRun && result.dry_run_report
    ? (result.dry_run_report.match(/\bBLOCK\b/g) ?? []).length
    : 0;
  const statusColor = ok && (!isDryRun || blockedCount === 0)
    ? 'text-[var(--color-status-success)]'
    : isDryRun && blockedCount > 0
    ? 'text-[var(--color-status-pending)]'
    : 'text-[var(--color-status-failure)]';

  return (
    <div className="space-y-4">
      {/* Header row */}
      <div className={clsx(
        'rounded-lg border p-4',
        isDryRun && blockedCount > 0
          ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
          : ok
          ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]'
          : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]',
      )}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={clsx('text-lg font-mono font-medium', statusColor)}>
              {ok && !isDryRun ? '✓' : isDryRun ? (blockedCount > 0 ? `⚠ ${blockedCount} blocked` : '✓ all pass') : '✗'}{' '}
              {result.status}
            </span>
            <span className="text-xs text-[var(--color-text-faint)] font-mono uppercase tracking-wider">{result.mode}</span>
          </div>
          <span className="text-xs text-[var(--color-text-faint)] font-mono">{result.duration_ms} ms</span>
        </div>
        <p className="mt-1 text-xs font-mono text-[var(--color-text-muted)] break-all">
          replay_id: {result.replay_id}
        </p>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: isDryRun ? 'model calls (skipped)' : 'model calls inspected', value: result.model_calls_mocked },
          { label: isDryRun ? 'tool calls checked' : 'tool calls inspected', value: result.tool_calls_mocked },
          { label: 'env warnings', value: result.env_warnings.length },
        ].map(({ label, value }) => (
          <div key={label} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 text-center">
            <span className="block font-mono text-xl text-[var(--color-text)]">{value}</span>
            <span className="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">{label}</span>
          </div>
        ))}
      </div>

      {/* Timing */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Timing</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">start</dt>
          <dd className="text-[var(--color-text-muted)]">{result.start_time}</dd>
          <dt className="text-[var(--color-text-faint)]">end</dt>
          <dd className="text-[var(--color-text-muted)]">{result.end_time}</dd>
          {result.exit_code != null && (
            <>
              <dt className="text-[var(--color-text-faint)]">exit_code</dt>
              <dd className={result.exit_code === 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>
                {result.exit_code}
              </dd>
            </>
          )}
        </dl>
      </div>

      {/* Policy flags */}
      {result.policy_flags_used.length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Policy flags</h4>
          <div className="flex flex-wrap gap-1.5">
            {result.policy_flags_used.map(f => (
              <code key={f} className="text-[10px] px-1.5 py-0.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] text-[var(--color-text-muted)]">{f}</code>
            ))}
          </div>
        </div>
      )}

      {/* Env warnings */}
      {result.env_warnings.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-2">
            Environment warnings ({result.env_warnings.length})
          </h4>
          <ul className="space-y-2 text-xs font-mono text-[var(--color-text-muted)]">
            {result.env_warnings.map((w, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-[var(--color-status-pending)] shrink-0">⚠</span>
                <span>{w.field ?? w.key ?? JSON.stringify(w)}: {w.message ?? w.detail ?? ''}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Dry-run policy report */}
      {isDryRun && result.dry_run_report && (
        <div className={clsx(
          'rounded-lg border p-4',
          blockedCount > 0
            ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]'
            : 'border-[var(--color-border)]',
          'bg-[var(--color-bg-raised)]',
        )}>
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">
            Policy report
            {blockedCount > 0 && (
              <span className="ml-2 text-[var(--color-status-pending)]">— {blockedCount} BLOCK{blockedCount !== 1 ? 'S' : ''}</span>
            )}
          </h4>
          <pre className="text-xs font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all leading-relaxed overflow-auto max-h-80">
            {result.dry_run_report}
          </pre>
        </div>
      )}

      {/* Error detail */}
      {result.error && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-failure)] font-mono mb-2">Error</h4>
          <pre className="text-xs font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all">
            {JSON.stringify(result.error, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

export function SemanticResultPane({ result }: { result: ReplayResult }) {
  const score = result.similarity_score ?? 0;
  const pct = Math.round(score * 100);
  const tone = pct >= 80 ? 'success' : pct >= 50 ? 'pending' : 'failure';
  const toneColor = tone === 'success'
    ? 'text-[var(--color-status-success)]'
    : tone === 'pending'
    ? 'text-[var(--color-status-pending)]'
    : 'text-[var(--color-status-failure)]';
  const toneBorder = tone === 'success'
    ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]'
    : tone === 'pending'
    ? 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]'
    : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]';

  return (
    <div className="space-y-4">
      <div className={clsx('rounded-lg border p-4', toneBorder)}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={clsx('text-2xl font-mono font-bold', toneColor)}>{pct}%</span>
            <span className="text-xs text-[var(--color-text-faint)] font-mono uppercase tracking-wider">semantic similarity</span>
          </div>
          <span className="text-xs text-[var(--color-text-faint)] font-mono">{result.duration_ms} ms</span>
        </div>
        <p className="mt-2 text-xs text-[var(--color-text-muted)]">
          Average pairwise similarity across {result.model_calls_mocked} model call response{result.model_calls_mocked === 1 ? '' : 's'}.
          {pct >= 80 ? ' High consistency — outputs are semantically stable.'
            : pct >= 50 ? ' Moderate variation — some responses differ in meaning.'
            : ' High variation — outputs diverge significantly across calls.'}
        </p>
        {result.matched_run_id && (
          <p className="mt-1 text-[10px] font-mono text-[var(--color-text-faint)] break-all">
            matched_run_id: {result.matched_run_id}
          </p>
        )}
      </div>

      {/* Similarity gauge */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-3">Similarity gauge</h4>
        <div className="relative h-3 rounded-full bg-[var(--color-bg-sunken)] overflow-hidden">
          <div
            className={clsx('h-full rounded-full transition-all', tone === 'success' ? 'bg-[var(--color-status-success)]' : tone === 'pending' ? 'bg-[var(--color-status-pending)]' : 'bg-[var(--color-status-failure)]')}
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className="flex justify-between mt-1 text-[var(--text-2xs)] font-mono text-[var(--color-text-faint)]">
          <span>0%</span><span>50%</span><span>100%</span>
        </div>
      </div>

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Replay metadata</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">replay_id</dt>
          <dd className="text-[var(--color-text-muted)] break-all">{result.replay_id}</dd>
          <dt className="text-[var(--color-text-faint)]">start</dt>
          <dd className="text-[var(--color-text-muted)]">{result.start_time}</dd>
          <dt className="text-[var(--color-text-faint)]">model_calls</dt>
          <dd className="text-[var(--color-text-muted)]">{result.model_calls_mocked}</dd>
        </dl>
      </div>

      {result.env_warnings.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-2">Environment warnings</h4>
          <ul className="space-y-1 text-xs font-mono text-[var(--color-text-muted)]">
            {result.env_warnings.map((w, i) => (
              <li key={i}><span className="text-[var(--color-status-pending)]">⚠ </span>{w.message ?? JSON.stringify(w)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function ExactResultPane({ result }: { result: ReplayResult }) {
  const eligible = result.exact_eligible ?? false;
  const reasons = result.exact_reasons ?? [];

  return (
    <div className="space-y-4">
      <div className={clsx(
        'rounded-lg border p-4',
        eligible
          ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]'
          : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]',
      )}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={clsx('text-lg font-mono font-medium', eligible ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
              {eligible ? '✓ eligible' : '✗ not eligible'}
            </span>
            <span className="text-xs text-[var(--color-text-faint)] font-mono uppercase tracking-wider">exact replay</span>
          </div>
          <span className="text-xs text-[var(--color-text-faint)] font-mono">{result.duration_ms} ms</span>
        </div>
        <p className="mt-2 text-xs text-[var(--color-text-muted)]">
          {eligible
            ? 'This capsule meets all requirements for exact replay: deterministic environment lock and seeded model calls.'
            : `This capsule cannot be replayed exactly. ${reasons.length} issue${reasons.length === 1 ? '' : 's'} found.`}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 text-center">
          <span className="block font-mono text-xl text-[var(--color-text)]">{result.exact_hash_count ?? 0}</span>
          <span className="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">inputs hashed</span>
        </div>
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 text-center">
          <span className={clsx('block font-mono text-xl', reasons.length === 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {reasons.length}
          </span>
          <span className="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">blockers</span>
        </div>
      </div>

      {reasons.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-failure)] font-mono mb-2">
            Blockers ({reasons.length})
          </h4>
          <ul className="space-y-2 text-xs font-mono text-[var(--color-text-muted)]">
            {reasons.map((r, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-[var(--color-status-failure)] shrink-0">✗</span>
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4">
        <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-2">Replay metadata</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--color-text-faint)]">replay_id</dt>
          <dd className="text-[var(--color-text-muted)] break-all">{result.replay_id}</dd>
          <dt className="text-[var(--color-text-faint)]">start</dt>
          <dd className="text-[var(--color-text-muted)]">{result.start_time}</dd>
          <dt className="text-[var(--color-text-faint)]">model_calls</dt>
          <dd className="text-[var(--color-text-muted)]">{result.model_calls_mocked}</dd>
        </dl>
      </div>

      {result.env_warnings.length > 0 && (
        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-4">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-2">Environment warnings</h4>
          <ul className="space-y-1 text-xs font-mono text-[var(--color-text-muted)]">
            {result.env_warnings.map((w, i) => (
              <li key={i}><span className="text-[var(--color-status-pending)]">⚠ </span>{w.message ?? JSON.stringify(w)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
