/**
 * Replay mode contracts for the Replay workspace (issue #269) — pure data + helpers.
 *
 * Every sentence here restates `nova replay --help` and docs/architecture/replay-modes.md;
 * nothing is inferred. `nova replay` has five modes (`mocked`, `forensic`, `semantic`,
 * `exact`, `intervention`); `mocked` is shown twice because its default (strict,
 * fail-closed) and `--permissive` contracts differ in what may run live. There is no
 * `live` mode.
 *
 * Which modes the dashboard may *start* is decided by the server
 * (`GET /api/runs/{id}/replayability` → `dashboard_modes`), and even then only modes
 * mapped to an existing confirm-gated run action below can be started. `mocked` and
 * `intervention` re-run the workload: they have no dashboard action at all, so the
 * workspace shows their command to copy.
 */
import type { RunAction } from '../dashboard/tabs/runs/types';

/** A `dashboard_modes` key the server reports. */
export type StartableKey = 'forensic' | 'dry_run' | 'semantic' | 'exact' | 'mocked' | 'intervention';

export type WorkspaceModeId =
  | 'forensic'
  | 'semantic'
  | 'exact'
  | 'mocked'
  | 'mocked-permissive'
  | 'intervention';

export interface ReplayModeContract {
  id: WorkspaceModeId;
  title: string;
  /** The `--mode` value. */
  cliMode: 'forensic' | 'semantic' | 'exact' | 'mocked' | 'intervention';
  /** Extra flags, in order, after `--mode`. */
  flags: readonly string[];
  /** Re-runs the captured command. */
  executesWorkload: boolean;
  /** Something may run live (a tool, a model call, the workload's own writes). */
  sideEffecting: boolean;
  executes: string;
  tools: string;
  network: string;
  writes: string;
  maturity: string;
  /** The confirm-gated Runs action that starts it from the dashboard, or null (copy only). */
  dashboardAction: Extract<RunAction, 'replay' | 'semantic' | 'exact'> | null;
  startableKey: StartableKey;
}

const NOTHING_RUNS = 'None — the command is not run, so no tool runs.';
const NO_NETWORK = 'Not used — nothing is executed.';
const RESULT_ONLY = 'Only the replay result (replay_result.yaml) in a new replay directory.';

export const REPLAY_MODES: readonly ReplayModeContract[] = [
  {
    id: 'forensic',
    title: 'Forensic',
    cliMode: 'forensic',
    flags: [],
    executesWorkload: false,
    sideEffecting: false,
    executes:
      'Nothing. Reads the capsule and reports call counts, environment differences and tool-schema drift.',
    tools: NOTHING_RUNS,
    network: NO_NETWORK,
    writes: RESULT_ONLY,
    maturity: 'works today',
    dashboardAction: 'replay',
    startableKey: 'forensic',
  },
  {
    id: 'semantic',
    title: 'Semantic',
    cliMode: 'semantic',
    flags: [],
    executesWorkload: false,
    sideEffecting: false,
    executes:
      'Nothing. Scores how similar the recorded model responses are to one another (text similarity 0.0–1.0, not a judgement of meaning). No model is called.',
    tools: NOTHING_RUNS,
    network: NO_NETWORK,
    writes: RESULT_ONLY,
    maturity: 'works today',
    dashboardAction: 'semantic',
    startableKey: 'semantic',
  },
  {
    id: 'exact',
    title: 'Exact eligibility',
    cliMode: 'exact',
    flags: [],
    executesWorkload: false,
    sideEffecting: false,
    executes:
      'Nothing. Checks whether a byte-exact re-run is possible: env.lock mode deterministic, a seed on every model call, no tool-schema drift.',
    tools: NOTHING_RUNS,
    network: NO_NETWORK,
    writes: RESULT_ONLY,
    maturity: 'works today',
    dashboardAction: 'exact',
    startableKey: 'exact',
  },
  {
    id: 'mocked',
    title: 'Mocked — strict (default)',
    cliMode: 'mocked',
    flags: [],
    executesWorkload: true,
    sideEffecting: true,
    executes:
      'Re-runs the recorded command in a subprocess (600 s timeout) and serves recorded model responses and recorded results of intercepted tools from the capsule. Fail-closed: any divergence fails the replay. Refused before anything runs when the capsule has no command to re-run, records a model or tool call on a surface replay cannot serve, or has a replay.yaml override replay cannot enforce.',
    tools:
      'Intercepted tools (MCP call_tool, record.tool functions, wrapped framework tools) are served from the capsule. Every other tool — HTTP, shell, files, undeclared functions — RUNS LIVE.',
    network:
      'Reachable. Connections are observed, not blocked; a connection past loopback fails a strict replay (unserved_network). Add --hermetic (experimental, Linux) to block the network.',
    writes:
      'Whatever the re-run workload and its live tools write, plus the replay result and a replay capsule.',
    maturity: 'works today (Python workloads, supported surfaces only)',
    dashboardAction: null,
    startableKey: 'mocked',
  },
  {
    id: 'mocked-permissive',
    title: 'Mocked — permissive',
    cliMode: 'mocked',
    flags: ['--permissive'],
    executesWorkload: true,
    sideEffecting: true,
    executes:
      'The same re-run, but divergences are only reported: a model call with no recorded response gets an empty reply, unsupported model surfaces run LIVE, an unmatched intercepted tool call runs live only if an --allow-* flag permits its mutation class (never if replay.yaml says allow: false), and unconsumed recordings are only reported.',
    tools:
      'As strict, plus unmatched intercepted tool calls when an --allow-* flag permits them. Non-intercepted tools RUN LIVE.',
    network:
      'Reachable, and used: calls on unsupported model surfaces reach their provider. Connections past loopback are counted, not failed.',
    writes:
      'Whatever the re-run workload and its live tools write, plus the replay result and a replay capsule.',
    maturity: 'works today (Python workloads, supported surfaces only)',
    dashboardAction: null,
    startableKey: 'mocked',
  },
  {
    id: 'intervention',
    title: 'Intervention',
    cliMode: 'intervention',
    flags: ['--intervention-file', 'spec.yaml'],
    executesWorkload: true,
    sideEffecting: true,
    executes:
      'Substitutes one recorded event (from the InterventionSpec file) and re-runs everything downstream under mocked semantics, then writes a counterfactual capsule you can diff. A capsule with no command to re-run gets the counterfactual streams without a re-run.',
    tools:
      'A substituted model response reaches the re-run; a substituted tool result does not — tools RUN LIVE.',
    network: 'Reachable, as in mocked mode.',
    writes:
      'Whatever the re-run workload and its live tools write, plus the replay result and a counterfactual capsule.',
    maturity: 'experimental',
    dashboardAction: null,
    startableKey: 'intervention',
  },
];

/** POSIX shell quoting: bare when safe, else single-quoted. */
export function shellQuote(arg: string): string {
  if (arg !== '' && /^[A-Za-z0-9_./:@%+=,-]+$/.test(arg)) return arg;
  return `'${arg.replace(/'/g, `'\\''`)}'`;
}

/** The exact `nova replay …` command for *mode* against the capsule at *capsuleRef*. */
export function replayCommand(mode: ReplayModeContract, capsuleRef: string): string {
  return ['nova', 'replay', shellQuote(capsuleRef), '--mode', mode.cliMode, ...mode.flags].join(' ');
}

/**
 * `nova replay <ref> --dry-run` — the default (strict mocked) mode's dry run: executes
 * nothing, prints `REFUSED:` and each `[LIVE]` / `[NOT SERVED]` call. Distinct from the
 * dashboard's dry-run action, which is `--mode forensic --dry-run` (tool policy only).
 */
export function mockedDryRunCommand(capsuleRef: string): string {
  return `nova replay ${shellQuote(capsuleRef)} --dry-run`;
}

/** Per startable mode: the scope its route requires and whether this credential holds it. */
export interface ModeAccess {
  required_scope: string | null;
  held_scope: string | null;
  permitted: boolean;
}

/**
 * Why this credential may not start *mode*, from the server's `mode_access`, or null.
 * Unknown access (an older server) is not a refusal: the route still enforces its scope,
 * and a denial is shown as the action's error.
 */
export function startBlockedReason(
  mode: ReplayModeContract,
  modeAccess: Record<string, ModeAccess> | null | undefined,
): string | null {
  const access = modeAccess?.[mode.startableKey];
  if (!access || access.permitted) return null;
  const needs = access.required_scope ? `the "${access.required_scope}" scope` : 'a scope this server does not report';
  const holds = access.held_scope ? `this credential holds "${access.held_scope}"` : 'this credential holds no recognised scope';
  return `Not permitted: starting it needs ${needs}; ${holds}. The command above still works in a terminal.`;
}

/**
 * The dashboard action for *mode*, or null when the dashboard must not start it.
 * Requires both a client-side action (none exists for modes that run the workload)
 * and the server's explicit `true` for the mode.
 */
export function startableAction(
  mode: ReplayModeContract,
  dashboardModes: Record<string, boolean> | null | undefined,
): ReplayModeContract['dashboardAction'] {
  if (mode.executesWorkload || mode.dashboardAction === null) return null;
  return dashboardModes?.[mode.startableKey] === true ? mode.dashboardAction : null;
}
