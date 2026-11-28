/**
 * Event model for the failure-first Run Explorer (issue #268, first slice).
 *
 * Turns the records `GET /api/runs/{id}` already serves (`model_calls`,
 * `transport_model_calls`, `tool_calls`, `manifest`) into one time-ordered
 * event list, and says for every event where it came from. Pure functions
 * only: no fetch, no React.
 *
 * Provenance is read from fields the capsule really carries, never guessed:
 *
 * - `recorded`           — a record written at capture time into
 *                          `model-calls.jsonl` / `tool-calls.jsonl`.
 * - `transport`          — an HTTP attempt the wire hook recorded inside an
 *                          SDK call (`extensions["io.novafabric.record_role"]
 *                          == "transport"`, ADR-0305): evidence of what went
 *                          over the wire, not a model call of its own.
 * - `replay-record`      — a record in a replay capsule (`manifest.replay_of_run_id`
 *                          set): written while a replay ran, not by the original run.
 * - `replay-placeholder` — a record a mocked replay wrote for a call it did not
 *                          serve from the original capsule
 *                          (`extensions["io.novafabric.replay_placeholder"]`,
 *                          ADR-0321): `live` (ran live, outcome not observed),
 *                          `refused` or `unmatched`.
 *
 * Anything this module computes — the ordering, the ok/failed/unknown outcome,
 * which event is the "first observed failure", the related-call joins — is
 * *derived* and labelled as such in the UI. "First observed failure" is the
 * earliest event by recorded `started_at` whose recorded status is not
 * `success`; it is not a root-cause claim.
 */
import type { FullCapsule, RunSummary } from '../../../../lib/api';

export type EventStream = 'model' | 'transport' | 'tool';
export type EventProvenance = 'recorded' | 'transport' | 'replay-record' | 'replay-placeholder';
export type EventOutcome = 'ok' | 'failed' | 'unknown';

export interface RunEvent {
  /** Stable key, also the `?event=` URL value: `<stream>:<record id or #line>`. */
  key: string;
  stream: EventStream;
  /** Zero-based position of the record in its stream file (file order). */
  index: number;
  /** `model_call_id` / `tool_call_id` as recorded, or null when absent. */
  recordId: string | null;
  /** Tool name, model id or endpoint — whatever the record names, else the stream. */
  label: string;
  startedAt: string | null;
  /** Parsed `started_at`, or null when absent or unparseable. */
  startedMs: number | null;
  durationMs: number | null;
  /** The recorded `status`, verbatim (null when the record has none). */
  status: string | null;
  outcome: EventOutcome;
  provenance: EventProvenance;
  /** `live` | `refused` | `unmatched` on a replay placeholder. */
  replayPlaceholder: string | null;
  logLevel: string | null;
  logLevelSource: string | null;
  statusMessage: string | null;
  error: { type: string | null; message: string | null } | null;
  /** Rule ids of every `[REDACTED:<rule>]` marker found in the record. */
  redactions: string[];
  parentSpanId: string | null;
  /** Tool call → the model call that chose it (`agent_call_id`). */
  agentCallId: string | null;
  /** Transport attempt → its logical SDK call (`io.novafabric.logical_call_id`). */
  logicalCallId: string | null;
  /** Model call → tool calls its choices triggered (`tool_choice_call_ids`). */
  toolChoiceIds: string[];
  record: Record<string, unknown>;
}

export interface RunEventsModel {
  /** Timed events in `started_at` order, then untimed ones in file order. */
  events: RunEvent[];
  /** How many events have no usable `started_at` (listed last). */
  untimed: number;
  /** Honest notes about evidence the list cannot show. */
  gaps: string[];
  /** Set when the capsule is a replay: the original run and the mode. */
  replay: { of: string; mode: string | null } | null;
}

const RECORD_ROLE = 'io.novafabric.record_role';
const LOGICAL_CALL_ID = 'io.novafabric.logical_call_id';
const REPLAY_PLACEHOLDER = 'io.novafabric.replay_placeholder';

/** Run statuses that mean the run did not finish successfully. */
const FAILED_RUN_STATUSES = new Set(['failure', 'failed', 'error', 'aborted', 'partial', 'timeout']);

/** True for a run status that ended badly (`partial` and `aborted` included). */
export function isFailedRunStatus(status: string | null | undefined): boolean {
  return FAILED_RUN_STATUSES.has((status ?? '').toLowerCase());
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null;
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function ext(rec: Record<string, unknown>): Record<string, unknown> {
  const e = rec.extensions;
  return e && typeof e === 'object' && !Array.isArray(e) ? (e as Record<string, unknown>) : {};
}

function parseMs(ts: string | null): number | null {
  if (!ts) return null;
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? ms : null;
}

const REDACTION_RE = /\[REDACTED:([A-Za-z0-9_.-]+)/g;
const MAX_WALK_DEPTH = 32;
const MAX_WALK_NODES = 20_000;

/**
 * Rule ids of every `[REDACTED:<rule>]` marker (ADR-0009 masking) anywhere in
 * `value`, deduplicated and sorted. The walk is bounded (depth and node count)
 * so a pathological record cannot stall the UI.
 */
export function findRedactions(value: unknown): string[] {
  const rules = new Set<string>();
  let nodes = 0;
  const walk = (v: unknown, depth: number): void => {
    if (++nodes > MAX_WALK_NODES || depth > MAX_WALK_DEPTH) return;
    if (typeof v === 'string') {
      if (!v.includes('[REDACTED:')) return;
      for (const m of v.matchAll(REDACTION_RE)) rules.add(m[1]);
    } else if (Array.isArray(v)) {
      for (const item of v) walk(item, depth + 1);
    } else if (v && typeof v === 'object') {
      for (const [k, item] of Object.entries(v as Record<string, unknown>)) {
        walk(k, depth + 1);
        walk(item, depth + 1);
      }
    }
  };
  walk(value, 0);
  return [...rules].sort();
}

function outcomeOf(status: string | null): EventOutcome {
  if (status === null) return 'unknown';
  return status.toLowerCase() === 'success' ? 'ok' : 'failed';
}

function errorOf(rec: Record<string, unknown>): RunEvent['error'] {
  const e = rec.error;
  if (!e || typeof e !== 'object' || Array.isArray(e)) return null;
  const o = e as Record<string, unknown>;
  const type = str(o.type);
  const message = str(o.message);
  return type || message ? { type, message } : null;
}

function toEvent(
  rec: Record<string, unknown>,
  stream: EventStream,
  index: number,
  isReplayCapsule: boolean,
): RunEvent {
  const e = ext(rec);
  const recordId = stream === 'tool' ? str(rec.tool_call_id) : str(rec.model_call_id);
  const placeholder = str(e[REPLAY_PLACEHOLDER]);
  const status = str(rec.status);
  const provenance: EventProvenance = placeholder
    ? 'replay-placeholder'
    : isReplayCapsule
      ? 'replay-record'
      : stream === 'transport'
        ? 'transport'
        : 'recorded';
  const label = stream === 'tool'
    ? (str(rec.tool_name) ?? 'tool call')
    : stream === 'transport'
      ? (str(rec.endpoint) ?? str(rec['gen_ai.request.model']) ?? 'HTTP attempt')
      : (str(rec['gen_ai.request.model']) ?? str(rec['gen_ai.system']) ?? 'model call');
  const startedAt = str(rec.started_at);
  const choices = Array.isArray(rec.tool_choice_call_ids)
    ? (rec.tool_choice_call_ids as unknown[]).filter((x): x is string => typeof x === 'string')
    : [];
  return {
    key: `${stream}:${recordId ?? `#${index}`}`,
    stream,
    index,
    recordId,
    label,
    startedAt,
    startedMs: parseMs(startedAt),
    durationMs: num(rec.duration_ms),
    status,
    outcome: outcomeOf(status),
    provenance,
    replayPlaceholder: placeholder,
    logLevel: str(rec.log_level),
    logLevelSource: str(rec.log_level_source),
    statusMessage: str(rec.status_message),
    error: errorOf(rec),
    redactions: findRedactions(rec),
    parentSpanId: str(rec.parent_span_id),
    agentCallId: str(rec.agent_call_id),
    logicalCallId: str(e[LOGICAL_CALL_ID]),
    toolChoiceIds: choices,
    record: rec,
  };
}

function records(v: unknown): Record<string, unknown>[] {
  if (!Array.isArray(v)) return [];
  return v.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object' && !Array.isArray(r));
}

/** Build the ordered, provenance-labelled event list for one capsule. */
export function buildRunEvents(capsule: FullCapsule): RunEventsModel {
  const manifest = (capsule.manifest ?? {}) as Record<string, unknown>;
  const replayOf = str(manifest.replay_of_run_id);
  const isReplay = replayOf !== null;
  const model = records(capsule.model_calls).map((r, i) => toEvent(r, 'model', i, isReplay));
  const transport = records(capsule.transport_model_calls).map((r, i) => toEvent(r, 'transport', i, isReplay));
  const tool = records(capsule.tool_calls).map((r, i) => toEvent(r, 'tool', i, isReplay));

  // Duplicate record ids would make two events share a key: suffix the later ones.
  const seen = new Map<string, number>();
  const all = [...model, ...transport, ...tool].map(ev => {
    const n = seen.get(ev.key) ?? 0;
    seen.set(ev.key, n + 1);
    return n === 0 ? ev : { ...ev, key: `${ev.key}~${n}` };
  });

  const streamRank: Record<EventStream, number> = { model: 0, transport: 1, tool: 2 };
  const timed = all.filter(ev => ev.startedMs !== null);
  const untimed = all.filter(ev => ev.startedMs === null);
  timed.sort((a, b) =>
    (a.startedMs as number) - (b.startedMs as number)
    || streamRank[a.stream] - streamRank[b.stream]
    || a.index - b.index,
  );

  const gaps: string[] = [];
  if (capsule.capsule_available === false) {
    gaps.push('The capsule files are not on disk: only indexed metadata is available, so no events can be listed.');
  }
  const declaredTools = num(manifest.tool_call_count);
  if (declaredTools !== null && capsule.capsule_available !== false && declaredTools !== tool.length) {
    gaps.push(
      `The manifest declares ${declaredTools} tool call${declaredTools === 1 ? '' : 's'}; `
      + `tool-calls.jsonl holds ${tool.length}.`,
    );
  }
  if (untimed.length > 0) {
    gaps.push(
      `${untimed.length} event${untimed.length === 1 ? ' has' : 's have'} no recorded start time; `
      + 'listed last, in file order.',
    );
  }
  const live = all.filter(ev => ev.replayPlaceholder === 'live').length;
  if (live > 0) {
    gaps.push(`${live} call${live === 1 ? '' : 's'} ran live during the replay; the outcome was not observed.`);
  }

  return {
    events: [...timed, ...untimed],
    untimed: untimed.length,
    gaps,
    replay: isReplay ? { of: replayOf as string, mode: str(manifest.replay_mode) } : null,
  };
}

const MODEL_CACHE = new WeakMap<FullCapsule, RunEventsModel>();

/** {@link buildRunEvents}, computed once per capsule object (the banner and the inspector share it). */
export function runEventsFor(capsule: FullCapsule): RunEventsModel {
  let model = MODEL_CACHE.get(capsule);
  if (!model) {
    model = buildRunEvents(capsule);
    MODEL_CACHE.set(capsule, model);
  }
  return model;
}

/**
 * The first observed failing event: earliest by recorded `started_at` (the
 * list order), whose recorded status is not `success`. Transport attempts are
 * skipped when they belong to a logical call — a retried attempt that the SDK
 * then recovered from is not the run's failure. Null when nothing failed.
 */
export function firstObservedFailure(events: readonly RunEvent[]): RunEvent | null {
  return events.find(ev => ev.outcome === 'failed' && !(ev.stream === 'transport' && ev.logicalCallId)) ?? null;
}

export interface RunFailure {
  status: string | null;
  exitCode: number | null;
  /** The manifest's recorded `error` (`type`, `message`), when present. */
  error: { type: string | null; message: string | null } | null;
}

/** Why a run failed, from its recorded status, exit code and manifest `error`. Null when it did not. */
export function runFailure(run: RunSummary | null, manifest: Record<string, unknown> | null | undefined): RunFailure | null {
  const m = manifest ?? {};
  const status = run?.status ?? str(m.status);
  const exitCode = run?.exit_code ?? num(m.exit_code);
  const failed = isFailedRunStatus(status) || (exitCode !== null && exitCode !== 0);
  if (!failed) return null;
  return { status, exitCode, error: errorOf(m) };
}

export const PROVENANCE_LABEL: Record<EventProvenance, string> = {
  recorded: 'Recorded',
  transport: 'Transport attempt',
  'replay-record': 'Recorded in replay',
  'replay-placeholder': 'Replay placeholder',
};

export const PROVENANCE_HELP: Record<EventProvenance, string> = {
  recorded: 'Written at capture time into the capsule; shown as stored.',
  transport: 'An HTTP attempt the wire hook recorded inside an SDK call. Evidence of what went over the wire; not counted as a model call.',
  'replay-record': 'Written while a replay of another run ran, not by the original run.',
  'replay-placeholder': 'Written by a mocked replay for a call it did not serve from the original capsule. It carries no copy of the original record.',
};

export const STREAM_LABEL: Record<EventStream, string> = {
  model: 'model call',
  transport: 'HTTP attempt',
  tool: 'tool call',
};
