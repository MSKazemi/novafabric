/**
 * ConfirmDialog wrappers for the register / eval / promote / rollback /
 * unregister lifecycle actions, plus the eval-history mini chart.
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 */
import { clsx } from 'clsx';
import type { AssetSummary, RegistrationSuggestion } from '../../../../lib/api';
import type { EvalHistoryEntry } from '../../EvalSparkline';
import { relativeTime } from '../../../../lib/time';
import ConfirmDialog from '../../ConfirmDialog';
import { validTargetsFor } from './lifecycle';

export function RegisterDialog({
  open,
  yaml,
  setYaml,
  suggestions,
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  yaml: string;
  setYaml: (v: string) => void;
  suggestions: RegistrationSuggestion[];
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ConfirmDialog
      open={open}
      title="Register a new asset"
      description="Register a model, prompt, tool, dataset, agent, or evaluation suite from a YAML spec. The asset starts in `development` and must pass eval before promotion."
      cliEquivalent="nova register <spec.yaml>"
      size="lg"
      details={
        <div className="space-y-3">
          {suggestions.length > 0 && (
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">
                Detected from recent runs — click to pre-fill
              </p>
              <div className="flex flex-wrap gap-1.5">
                {suggestions.map((s, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => setYaml(s.draft_spec_yaml)}
                    title={`${Math.round(s.confidence * 100)}% confidence · ${s.call_count} calls`}
                    className="inline-flex items-center gap-1 px-2 py-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] font-mono transition-colors"
                  >
                    <span className="text-[var(--color-text-faint)] uppercase tracking-wider">{s.asset_type}</span>
                    <span className="text-[var(--color-text)]">{s.detected_name}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">YAML spec</span>
            <textarea
              value={yaml}
              onChange={e => setYaml(e.target.value)}
              placeholder={`name: my-prompt\nversion: 0.1.0\nasset_type: prompt\n...`}
              rows={10}
              className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-xs focus:border-[var(--color-accent)] focus:outline-none"
            />
          </label>
        </div>
      }
      confirmLabel="Register"
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}

export function EvalDialog({
  target,
  history,
  historyLoading,
  busy,
  onConfirm,
  onCancel,
}: {
  target: AssetSummary | null;
  history: EvalHistoryEntry[] | null;
  historyLoading: boolean;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ConfirmDialog
      open={!!target}
      title="Run evaluation suites"
      description={target ? `Run all evaluation suites configured for this asset. Results are written to the registry and used to gate promotion.` : ''}
      cliEquivalent={target ? `nova eval ${target.name}@${target.version}` : ''}
      details={target && (
        <div className="mt-3 space-y-1.5">
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-semibold">
            Eval history
          </span>
          {historyLoading && (
            <p className="text-[10px] text-[var(--color-text-faint)]">Loading history…</p>
          )}
          {!historyLoading && history !== null && history.length === 0 && (
            <p className="text-[10px] text-[var(--color-text-faint)]">
              No eval runs recorded yet for this asset.
            </p>
          )}
          {!historyLoading && history && history.length > 0 && (
            <EvalHistoryChart history={history} />
          )}
        </div>
      )}
      confirmLabel="Run eval"
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}

export function PromoteDialog({
  target,
  promoteTo,
  setPromoteTo,
  force,
  setForce,
  busy,
  error,
  setError,
  onConfirm,
  onCancel,
}: {
  target: AssetSummary | null;
  promoteTo: string;
  setPromoteTo: (v: string) => void;
  force: boolean;
  setForce: (v: boolean) => void;
  busy: boolean;
  error: string | null;
  setError: (v: string | null) => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ConfirmDialog
      open={!!target}
      title="Promote asset"
      description={target ? `Move ${target.name}@${target.version} from ${target.status} to a new lifecycle stage.${target.asset_type === 'agent' ? ' Agent assets must have a passing eval unless --force is used.' : ''}` : ''}
      cliEquivalent={target ? `nova promote ${target.name}@${target.version} --to ${promoteTo}${force ? ' --force' : ''}` : ''}
      details={target && (() => {
        const validTargets = validTargetsFor(target.status);
        return (
          <div className="space-y-2">
            <div className="block">
              <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Target status</span>
              <div className="mt-1 flex rounded border border-[var(--color-border)] overflow-hidden text-xs font-mono">
                {(['staging', 'production', 'archived'] as const).map((s) => {
                  const isValid = validTargets.includes(s);
                  return (
                    <button
                      key={s}
                      type="button"
                      disabled={!isValid}
                      onClick={() => { setPromoteTo(s); setError(null); }}
                      className={clsx(
                        'flex-1 py-2 transition-colors',
                        !isValid && 'opacity-30 cursor-not-allowed',
                        isValid && promoteTo === s
                          ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                          : isValid
                            ? 'bg-[var(--color-bg)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)]'
                            : 'bg-[var(--color-bg)] text-[var(--color-text-muted)]',
                      )}
                    >{s}</button>
                  );
                })}
              </div>
            </div>
            {error && (
              <p className="text-[11px] text-[var(--color-status-failure)] rounded bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] px-2 py-1">{error}</p>
            )}
            <label className="flex items-center gap-2 text-[var(--color-text-muted)]">
              <input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} className="accent-[var(--color-status-failure)]" />
              <span><strong>Force</strong> — bypass the eval-passing gate (only for agents). Audit log records this clearly.</span>
            </label>
          </div>
        );
      })()}
      confirmLabel={`Promote → ${promoteTo}`}
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
      tone={force ? 'destructive' : 'default'}
    />
  );
}

export function RollbackDialog({
  target,
  reason,
  setReason,
  busy,
  onConfirm,
  onCancel,
}: {
  target: AssetSummary | null;
  reason: string;
  setReason: (v: string) => void;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ConfirmDialog
      open={!!target}
      title="Roll back asset"
      description={target ? `Archive the current production version of ${target.name}@${target.version} and restore the most recent previous production version. This action is recorded in the audit trail.` : ''}
      cliEquivalent={target ? `nova rollback ${target.name}` : ''}
      details={target && (
        <label className="block mt-2">
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Reason (optional)</span>
          <input
            type="text"
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder="e.g. regression in production"
            className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-xs focus:border-[var(--color-accent)] focus:outline-none"
          />
        </label>
      )}
      confirmLabel="Roll back"
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
      tone="destructive"
    />
  );
}

export function UnregisterDialog({
  target,
  force,
  setForce,
  busy,
  onConfirm,
  onCancel,
}: {
  target: AssetSummary | null;
  force: boolean;
  setForce: (v: boolean) => void;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ConfirmDialog
      open={!!target}
      title="Delete asset"
      description={target ? `Permanently remove ${target.name}@${target.version} from the registry. This cannot be undone. Only development and archived assets can be deleted without --force.` : ''}
      cliEquivalent={target ? `nova unregister ${target.name}@${target.version}${force ? ' --force' : ''}` : ''}
      details={target && (
        <label className="flex items-center gap-2 text-[var(--color-text-muted)] mt-2">
          <input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} className="accent-[var(--color-status-failure)]" />
          <span><strong>Force</strong> — bypass status guard (for staging/production/pending_approval). Use with caution.</span>
        </label>
      )}
      confirmLabel="Delete asset"
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
      tone="destructive"
    />
  );
}

// Private sub-component — only used in the eval confirm dialog above.
function EvalHistoryChart({ history }: { history: EvalHistoryEntry[] }) {
  const ordered = [...history].reverse(); // API returns newest-first; reverse to oldest-left
  const barW = 24;
  const gap = 3;
  const maxH = 64;
  const svgW = Math.max(1, ordered.length * (barW + gap) - gap);
  const svgH = 94; // bar area (64) + suite label (10) + timestamp (12) + score label buffer (8)

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg width={svgW} height={svgH} aria-label="Eval history chart">
        {ordered.map((entry, i) => {
          const x = i * (barW + gap);
          const barH = entry.score !== null ? Math.max(4, entry.score * maxH) : maxH;
          const barY = maxH - barH;
          const fill = entry.passed
            ? 'var(--color-status-success)'
            : 'var(--color-status-failure)';
          const suiteTrunc = entry.suite_name.length > 8
            ? `${entry.suite_name.slice(0, 7)}…`
            : entry.suite_name;

          return (
            <g key={entry.eval_id}>
              <rect x={x} y={barY} width={barW} height={barH} fill={fill} rx={2} />
              {entry.score !== null && (
                <text
                  x={x + barW / 2}
                  y={barY - 2}
                  textAnchor="middle"
                  fontSize={8}
                  fill="var(--color-text-muted)"
                >
                  {entry.score.toFixed(1)}
                </text>
              )}
              <text
                x={x + barW / 2}
                y={maxH + 10}
                textAnchor="middle"
                fontSize={8}
                fill="var(--color-text-faint)"
              >
                {suiteTrunc}
              </text>
              <text
                x={x + barW / 2}
                y={maxH + 20}
                textAnchor="middle"
                fontSize={7}
                fill="var(--color-text-faint)"
              >
                {relativeTime(entry.run_at)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
