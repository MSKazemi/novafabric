// Docker Runner Card (static). Extracted verbatim from InfraTab.tsx
// (dashboard-modernization split).
import { BADGE_COLOR, BADGE_LABEL, CmdBadge } from './badges';

export default function DockerRunnerCard() {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">Docker Runner</h3>
            <span className={`text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
              {BADGE_LABEL['partial']}
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">v0.6 — ADR-0025</div>
        </div>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Runs <code className="font-mono">nova capture</code> workloads inside a <code className="font-mono">docker run</code> container. Mounts the capsule directory as a volume; forwards stdout/stderr. Requires the image to have novafabric pip-installed.
      </p>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">Shipped</div>
        <ul className="space-y-0.5">
          {[
            'image — required: fully-qualified image reference',
            'network — docker network (default: bridge)',
            'workdir — container working directory',
            'user — run as uid:gid (e.g. 1000:1000)',
            'extra_volumes — list of host:container[:opts] mounts',
            'extra_env — dict of env vars injected alongside NOVAFABRIC_*',
            'Eval containers: OCI digest-pinned via run_eval_container (ADR-0033)',
            'Anti-patterns enforced: no --privileged, no host namespaces, no socket mount, no hardcoded registry',
          ].map((s, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[var(--color-text-muted)]">
              <span className="shrink-0 mt-px text-[var(--color-status-success)]">✓</span>
              {s}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-[11px] leading-relaxed">
        <span className="font-medium text-[var(--color-text)]">Dashboard: </span>
        <span className="text-[var(--color-text-muted)]">
          Docker runner options are surfaced in the Commands tab &rarr; nova capture &rarr; runner=docker.
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-[10px] text-[var(--color-text-faint)]">CLI:</span>
        <CmdBadge cmd="nova capture --runner docker" />
        <CmdBadge cmd="nova eval run" />
      </div>

      <p className="text-[11px] text-[var(--color-text-faint)] italic leading-relaxed border-l-2 border-[var(--color-border)] pl-2">
        Configure defaults in <code className="font-mono">.novafabric/runners.yaml</code> to avoid repeating <code className="font-mono">--runner-option</code> on every invocation.
      </p>
    </div>
  );
}
