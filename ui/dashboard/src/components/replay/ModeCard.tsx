/**
 * One replay mode's contract, in words, with its exact `nova replay` command (issue #269).
 *
 * A mode that re-runs the workload is marked side-effecting and is copy-only: the card
 * offers no way to start it. A read-only mode offers "Run in dashboard…" only when the
 * server says it can (`dashboard_modes`), and that button opens the Runs tab's existing
 * confirmation dialog — nothing starts on a single click or on opening the view.
 */
import { useId } from 'react';
import { clsx } from 'clsx';
import Badge from '../ui/primitives/Badge';
import type { ReplayabilitySummary } from '../../lib/api';
import type { ReplayModeContract } from './modes';

export interface ModeCardProps {
  mode: ReplayModeContract;
  command: string;
  /** Strict-mocked dry-run command, shown on modes that re-run the workload. */
  checkCommand: string | null;
  /** Current classification, or null when unknown. */
  summary: ReplayabilitySummary | null;
  /** Set when the dashboard may start this mode; opens the confirm dialog. */
  onStart: (() => void) | null;
  /** Why starting is refused for this credential (the server's scope check), if it is. */
  startBlockedReason?: string | null;
  onCopy: (command: string, what: string) => void;
}

/** What the replayability report means for this mode — never more than it says. */
export function servabilityLine(mode: ReplayModeContract, summary: ReplayabilitySummary | null): string {
  if (!mode.executesWorkload) return 'Does not depend on servability: nothing is re-run.';
  if (mode.cliMode === 'intervention') {
    return 'The servability check does not apply to intervention; it re-runs downstream under mocked semantics.';
  }
  if (!summary) return 'Unknown: the replayability report is not available.';
  const notServable = summary.model_calls?.not_servable ?? 0;
  if (mode.flags.includes('--permissive')) {
    if (summary.reexecution && summary.reexecution.reexecutable === false) {
      return 'Would be refused: the capsule cannot be re-run (--permissive does not change that).';
    }
    // `not_servable` counts both an unsupported surface (runs LIVE under --permissive) and a
    // supported surface whose response was never recorded (gets an empty reply); the
    // report does not split them, so neither is claimed.
    return notServable > 0
      ? `Would start. ${notServable} recorded model call${notServable === 1 ? ' is' : 's are'} not servable: if the workload makes ${notServable === 1 ? 'it' : 'them'}, a call on an unsupported surface runs LIVE and one on a supported surface with no recorded response gets an empty reply. The report does not say which applies.`
      : 'Would start; divergences are reported instead of failing the replay.';
  }
  if (summary.strict_mocked === 'refuse') {
    const type = summary.refusal?.type ? ` (${String(summary.refusal.type)})` : '';
    return `Would be refused before anything runs${type} — see the report above.`;
  }
  return summary.reasons.length > 0
    ? 'Would start, but not every recorded call is served — see the report above.'
    : 'Would start and serve every recorded call.';
}

export default function ModeCard({ mode, command, checkCommand, summary, onStart, startBlockedReason, onCopy }: ModeCardProps) {
  const titleId = useId();
  return (
    <li>
      <article
        aria-labelledby={titleId}
        className={clsx(
          'rounded-lg border bg-[var(--color-bg-raised)] p-4 space-y-3 h-full',
          mode.sideEffecting
            ? 'border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)]'
            : 'border-[var(--color-border)]',
        )}
      >
        <header className="flex flex-wrap items-center gap-2">
          <h5 id={titleId} className="text-sm font-medium text-[var(--color-text)]">{mode.title}</h5>
          {mode.sideEffecting
            ? <Badge tone="danger" dot>side-effecting · runs the workload</Badge>
            : <Badge tone="success">executes nothing</Badge>}
          <Badge tone={mode.maturity === 'experimental' ? 'pending' : 'neutral'}>{mode.maturity}</Badge>
        </header>
        <dl className="space-y-2 text-xs">
          {([
            ['What it executes', mode.executes],
            ['Tools', mode.tools],
            ['Network', mode.network],
            ['Writes', mode.writes],
            ['This capsule', servabilityLine(mode, summary)],
          ] as const).map(([term, text]) => (
            <div key={term}>
              <dt className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">{term}</dt>
              <dd className="mt-0.5 text-[var(--color-text-muted)]">{text}</dd>
            </div>
          ))}
        </dl>
        <div className="space-y-2">
          <CommandRow label="Command" command={command} onCopy={() => onCopy(command, `${mode.title} command`)} />
          {checkCommand && (
            <CommandRow
              label="Check first (executes nothing)"
              command={checkCommand}
              onCopy={() => onCopy(checkCommand, 'dry-run command')}
            />
          )}
        </div>
        {onStart && startBlockedReason ? (
          <div className="space-y-1">
            <button
              type="button"
              disabled
              aria-describedby={`${titleId}-blocked`}
              aria-label={`Run ${mode.title} in the dashboard (not permitted)`}
              className="px-2.5 py-1 text-xs rounded border border-[var(--color-border)] text-[var(--color-text-faint)] opacity-60 cursor-not-allowed"
            >
              Run in dashboard…
            </button>
            <p id={`${titleId}-blocked`} className="text-xs text-[var(--color-text-faint)]">{startBlockedReason}</p>
          </div>
        ) : onStart ? (
          <button
            type="button"
            onClick={onStart}
            aria-label={`Run ${mode.title} in the dashboard (asks for confirmation)`}
            className="px-2.5 py-1 text-xs rounded border border-[var(--color-border)] text-[var(--color-text)] hover:border-[var(--color-border-strong)]"
          >
            Run in dashboard…
          </button>
        ) : (
          <p className="text-xs text-[var(--color-text-faint)]">
            {mode.executesWorkload
              ? 'Copy only — the dashboard cannot start this mode: it re-runs the workload, and the dashboard server has no endpoint that does. Run it in a terminal, after the check above.'
              : 'Copy only — the dashboard server does not report this mode as startable.'}
          </p>
        )}
      </article>
    </li>
  );
}

function CommandRow({ label, command, onCopy }: { label: string; command: string; onCopy: () => void }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono mb-1">{label}</p>
      <div className="flex items-start gap-2">
        <code className="flex-1 min-w-0 break-all rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] px-2 py-1 font-mono text-xs text-[var(--color-text)] select-all">
          {command}
        </code>
        <button
          type="button"
          onClick={onCopy}
          aria-label={`Copy: ${command}`}
          className="shrink-0 px-2 py-1 text-xs rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
        >
          Copy
        </button>
      </div>
    </div>
  );
}
