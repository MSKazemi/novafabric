/**
 * Replay workspace for one run (issue #269, first slice).
 *
 * Shows, per replay mode, what it executes, whether tools run live, whether the
 * network is reachable, what this capsule's replayability report says, and the exact
 * `nova replay` command to copy. Opening it only reads (`GET …/replayability`); it
 * never starts a replay. Modes that execute nothing can be started through the Runs
 * tab's confirm dialog when the server reports them startable; modes that re-run the
 * workload are copy-only.
 */
import { useCallback, useEffect, useId, useState } from 'react';
import { api } from '../../lib/api';
import type { ReplayabilityView, RunSummary } from '../../lib/api';
import type { RunAction } from '../dashboard/tabs/runs/types';
import ModeCard from './ModeCard';
import ReplayabilityReport from './ReplayabilityReport';
import { REPLAY_MODES, mockedDryRunCommand, replayCommand, startableAction } from './modes';

export interface ReplayWorkspaceProps {
  run: RunSummary;
  /** The capsule directory; commands address it by path, which works from any directory. */
  capsulePath: string;
  /** Opens the Runs tab's confirmation dialog for a read-only replay action. */
  onAction: (run: RunSummary, action: RunAction) => void;
  /** Injectable for tests; defaults to the server API. */
  load?: (runId: string) => Promise<ReplayabilityView>;
}

export default function ReplayWorkspace({ run, capsulePath, onAction, load = api.getReplayability }: ReplayWorkspaceProps) {
  const headingId = useId();
  const [state, setState] = useState<{ runId: string; view: ReplayabilityView | null; error: string | null } | null>(null);
  const [tick, setTick] = useState(0);
  const [copyMessage, setCopyMessage] = useState('');

  useEffect(() => {
    let cancelled = false;
    setState(null);
    load(run.run_id).then(
      view => { if (!cancelled) setState({ runId: run.run_id, view, error: null }); },
      (e: unknown) => {
        if (!cancelled) setState({ runId: run.run_id, view: null, error: e instanceof Error ? e.message : String(e) });
      },
    );
    return () => { cancelled = true; };
  }, [run.run_id, load, tick]);

  // Never show another run's report (a late response for the previous selection).
  const current = state?.runId === run.run_id ? state : null;
  const view = current?.view ?? null;
  const summary = view?.current ?? null;
  const ref = capsulePath || run.run_id;

  const onCopy = useCallback(async (command: string, what: string) => {
    try {
      await navigator.clipboard.writeText(command);
      setCopyMessage(`Copied the ${what}.`);
    } catch {
      setCopyMessage('Clipboard unavailable. Select the command text and copy it manually.');
    }
  }, []);

  return (
    <section aria-labelledby={headingId} className="p-4 space-y-4">
      <div className="space-y-1">
        <h4 id={headingId} className="text-sm font-medium text-[var(--color-text)]">Replay workspace</h4>
        <p className="text-xs text-[var(--color-text-muted)] max-w-prose">
          What each <code className="font-mono">nova replay</code> mode would do with this run. Opening this view
          only reads the capsule — it starts nothing. The dashboard can start only the modes that execute nothing,
          and asks for confirmation first. Modes that re-run the workload are marked side-effecting and are
          copy-only: run them in a terminal.
        </p>
      </div>

      <ReplayabilityReport
        view={view}
        loading={current === null}
        error={current?.error ?? null}
        onRetry={() => setTick(t => t + 1)}
      />

      <ul aria-label="Replay modes" className="grid gap-3 lg:grid-cols-2">
        {REPLAY_MODES.map(mode => {
          const action = startableAction(mode, view?.dashboard_modes);
          return (
            <ModeCard
              key={mode.id}
              mode={mode}
              command={replayCommand(mode, ref)}
              checkCommand={mode.cliMode === 'mocked' ? mockedDryRunCommand(ref) : null}
              summary={summary}
              onStart={action ? () => onAction(run, action) : null}
              onCopy={onCopy}
            />
          );
        })}
      </ul>

      <p className="text-xs text-[var(--color-text-faint)] max-w-prose">
        There is no <code className="font-mono">live</code> mode: <code className="font-mono">nova replay</code> calls
        a model or runs a tool live only where a mode above says so. The dashboard&apos;s
        {' '}<em>Dry-run policy check</em> action is <code className="font-mono">--mode forensic --dry-run</code> (tool
        policy decisions only); the mocked-mode check above is the CLI&apos;s <code className="font-mono">--dry-run</code>.
      </p>

      <p role="status" aria-live="polite" className="text-xs text-[var(--color-text-muted)] min-h-[1rem]">{copyMessage}</p>
    </section>
  );
}
