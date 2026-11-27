/**
 * Alert / incident triage model (issue #276, experimental).
 *
 * Pure functions that turn one alert row or one incident record into what the
 * triage panel shows: the run(s) the record itself links to, a timeline in
 * words, and the next safe actions as copyable CLI commands.
 *
 * Honesty rules this module holds:
 *  - A run is linked (and loaded) only when the record names it authoritatively:
 *    `IncidentView.run_ids`. It is never derived from an alert's subject — a
 *    seal alert's `capsule:<id>` is a digest of the signed payload, not a run id.
 *  - A seal alert's `claimed_run_id` comes from the FAILED capsule's own
 *    capsule.yaml, which the failed seal no longer vouches for: a tampered
 *    capsule can name an innocent run. It is shown labelled unverified, never
 *    loaded as the subject, and commands target the capsule that failed.
 *  - A run id that is not a plain ref (only dots, a slash, …) is never put into
 *    a request path or a link: `/api/runs/..` would resolve to another route.
 *  - Verification status is what the record says (a seal alert *is* a recorded
 *    failure); otherwise "not checked here". The panel never runs a check.
 *  - Every command uses only subcommands and flags verified against `--help`
 *    (and present in the generated command catalog; a unit test pins that).
 */
import type { AlertRow, IncidentView } from '../../../lib/api';
import { shellQuote } from '../commands/commandRegistry';

export interface TimelineEntry {
  /** ISO timestamp, or null when the record holds no time for this step. */
  at: string | null;
  text: string;
}

export interface SafeCommand {
  /** The command's subcommand path, e.g. `nova verify` — checked against the catalog. */
  name: string;
  /** Flags used, e.g. `['--json']` — checked against the catalog. */
  flags: string[];
  /** Full command line, shell-quoted; `capsuleDir` placeholders filled by `renderCommand`. */
  template: string;
  /** What it does, in words — including what it writes, if anything. */
  purpose: string;
  /** Run id whose capsule directory fills the placeholder, when the command needs one. */
  needsCapsuleDir?: string;
}

export type VerifyState =
  | { kind: 'failed-recorded'; at: string; source: string }
  | { kind: 'not-checked' };

export interface TriageModel {
  kind: 'alert' | 'incident';
  /** Short human title for the panel heading. */
  title: string;
  /** The record's own id (alert event/audit id, incident id). */
  recordId: string;
  /** Run ids the record links to, in record order (deduplicated). */
  runIds: string[];
  /** Why no run is linked — set when `runIds` is empty and no run is claimed. */
  noRunReason: string | null;
  /** A run the record only *claims* (seal alert: the failed capsule's manifest). Unverified. */
  claimedRun: { runId: string; caveat: string } | null;
  /** The capsule that failed verification (seal alerts only). */
  failedCapsule: { capsuleId: string | null; dir: string | null } | null;
  /** Run ids the record carries that are not plain refs; never loaded or linked. */
  unsafeRunIds: string[];
  /** Extra context for the no-run case (what the subject is, how to link next time). */
  noRunHint: string | null;
  verify: VerifyState;
  firstDivergence: AlertRow['first_divergence'] | null;
  timeline: TimelineEntry[];
  commands: SafeCommand[];
}

/** Placeholder token replaced by a run's capsule directory once it is known. */
export const CAPSULE_DIR_TOKEN = '{{capsule-dir}}';

/** Shown wherever a claimed run id appears. */
export const CLAIM_CAVEAT =
  "Named by the failed capsule's own capsule.yaml, which the failed seal does not vouch for — " +
  'a tampered capsule can name any run. Verify independently before treating it as the affected run.';

const RUN_ID_RE = /^(?!\.+$)[A-Za-z0-9._:@-]{1,200}$/;

/** A plain run ref: safe in a URL path segment (no slash, never only dots). */
export function isSafeRunId(id: unknown): id is string {
  return typeof id === 'string' && RUN_ID_RE.test(id);
}

const EVENT_WORDS: Record<string, string> = {
  'ops.quota.breached': 'a capsule write was rejected by a hard storage quota',
  'ops.rate_limit.sustained': 'a client stayed over its rate limit',
  'ops.policy.violation': 'a policy gate denied an action',
  'ops.drift.detected': 'a drift statistic crossed its threshold',
  'ops.seal.verify_failed': '`nova verify` found a capsule whose seal does not verify',
  'ops.backup.failed': 'a backup create or verify failed',
};

/** Plain-words description of an `ops.*` alert type; the raw type for unknown ones. */
export function describeEvent(eventType: string): string {
  return EVENT_WORDS[eventType] ?? `an event of type ${eventType} was raised`;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

function divergenceSentence(fd: NonNullable<AlertRow['first_divergence']>): string {
  const total = fd.divergent_files;
  const others = total !== null && total > 1 ? `; ${plural(total, 'evidence file')} diverge in total` : '';
  return (
    `The first evidence file that no longer matches the seal is ${fd.path} (${fd.reason || 'changed'})${others}. ` +
    'The seal holds one digest per file, so no record inside the file is named.'
  );
}

/** Commands for one linked run, by run id (capsule directory filled in later). */
function runCommands(runId: string): SafeCommand[] {
  const zip = shellQuote(`${runId}-evidence.zip`);
  return [
    {
      name: 'nova verify',
      flags: ['--json'],
      template: `nova verify ${CAPSULE_DIR_TOKEN} --json`,
      purpose:
        `Re-check run ${runId}'s seal, timestamp and log inclusion. Reads only; if alerting is ` +
        'configured, a failed check raises a new alert.',
      needsCapsuleDir: runId,
    },
    {
      name: 'nova export-evidence',
      flags: ['--output'],
      template: `nova export-evidence ${CAPSULE_DIR_TOKEN} --output ${zip}`,
      purpose: `Write a new signed Evidence Bundle ZIP for run ${runId}. Changes nothing in the capsule.`,
      needsCapsuleDir: runId,
    },
  ];
}

function diffCommand(a: string, b: string): SafeCommand {
  return {
    name: 'nova diff',
    flags: ['--json'],
    template: `nova diff ${shellQuote(a)} ${shellQuote(b)} --json`,
    purpose: `Compare runs ${a} and ${b}; the JSON names the first divergent step. Read-only.`,
  };
}

/** Fill a command template with a run's capsule directory, or a visible placeholder. */
export function renderCommand(cmd: SafeCommand, capsuleDir: string | null): string {
  if (!cmd.template.includes(CAPSULE_DIR_TOKEN)) return cmd.template;
  const dir = capsuleDir ? shellQuote(capsuleDir) : `<capsule-dir-of-${cmd.needsCapsuleDir ?? 'run'}>`;
  return cmd.template.split(CAPSULE_DIR_TOKEN).join(dir);
}

function unique(ids: Array<string | null | undefined>): string[] {
  const out: string[] = [];
  for (const id of ids) {
    if (typeof id === 'string' && id.trim() && !out.includes(id)) out.push(id);
  }
  return out;
}

export function alertTriage(row: AlertRow): TriageModel {
  const isSeal = row.event_type === 'ops.seal.verify_failed';
  const claimedId = isSafeRunId(row.claimed_run_id) ? row.claimed_run_id : null;
  const unsafeRunIds = typeof row.claimed_run_id === 'string' && row.claimed_run_id && !claimedId
    ? [row.claimed_run_id] : [];
  const capsuleDir = typeof row.capsule_dir === 'string' && row.capsule_dir ? row.capsule_dir : null;
  const capsuleId = row.subject.startsWith('capsule:') ? row.subject.slice('capsule:'.length) || null : null;
  const sev = row.severity ?? 'no severity';
  const timeline: TimelineEntry[] = [];
  const what = `${describeEvent(row.event_type)} (${row.event_type}, ${sev}), about ${row.subject || 'no recorded subject'}`;
  if (row.outcome === 'emitted') {
    timeline.push({ at: row.timestamp, text: `Raised: ${what}. Recorded in the local events log; no delivery attempt is recorded for it.` });
  } else {
    timeline.push({ at: null, text: `Raised: ${what}. The feed keeps the delivery time only, not the time it was raised.` });
    const where = row.endpoint_id ? `endpoint ${row.endpoint_id}` : 'an endpoint';
    const tries = plural(row.attempts, 'attempt');
    const outcome =
      row.outcome === 'delivered' ? `Delivered to ${where} after ${tries}.`
        : row.outcome === 'failed' ? `Delivery to ${where} failed after ${tries}.`
          : `Delivery outcome for ${where}: ${row.outcome}.`;
    timeline.push({ at: row.timestamp, text: outcome });
  }
  if (capsuleDir) timeline.push({ at: null, text: `The capsule that failed verification is ${capsuleDir}.` });
  if (claimedId) {
    timeline.push({
      at: null,
      text: `Its own capsule.yaml names run ${claimedId} — unverified: the failed seal does not vouch for that manifest.`,
    });
  }
  if (row.first_divergence) {
    timeline.push({ at: null, text: divergenceSentence(row.first_divergence) });
  }

  const commands: SafeCommand[] = [];
  if (isSeal) {
    const target = capsuleDir ? shellQuote(capsuleDir) : `<capsule-dir-of-capsule:${capsuleId ?? 'unknown'}>`;
    commands.push({
      name: 'nova verify',
      flags: ['--json'],
      template: `nova verify ${target} --json`,
      purpose:
        'Re-check the capsule that failed (the directory `nova verify` reported) — not the run its manifest ' +
        'names. Reads only; if alerting is configured, a failed check raises a new alert.',
    });
  }
  commands.push({
    name: 'nova audit-log export',
    flags: ['--since'],
    template: `nova audit-log export --since ${shellQuote(row.timestamp)}`,
    purpose:
      "Print the hash-chained audit log from this alert's time onward (JSONL to stdout): delivery " +
      'records and other audited actions. Read-only.',
  });

  const noRun = !claimedId;
  let noRunHint: string | null = null;
  if (noRun) {
    noRunHint = isSeal
      ? `The subject ${row.subject} names the seal's capsule id — a digest of the signed payload, not a run id — so no run is guessed from it. Seal alerts raised by this version record the failed capsule's directory and the run its manifest claims (unverified); older alerts record neither.`
      : 'This alert type is about a system condition, not one run.';
  }

  return {
    kind: 'alert',
    title: row.event_type,
    recordId: row.id,
    runIds: [],
    noRunReason: noRun ? 'No run linked — the alert source did not record a run id.' : null,
    noRunHint,
    claimedRun: claimedId ? { runId: claimedId, caveat: CLAIM_CAVEAT } : null,
    failedCapsule: isSeal ? { capsuleId, dir: capsuleDir } : null,
    unsafeRunIds,
    verify: isSeal ? { kind: 'failed-recorded', at: row.timestamp, source: 'nova verify' } : { kind: 'not-checked' },
    firstDivergence: row.first_divergence ?? null,
    timeline,
    commands,
  };
}

export function incidentTriage(inc: IncidentView, nowMs: number = Date.now()): TriageModel {
  const all = unique(inc.run_ids ?? []);
  const runIds = all.filter(isSafeRunId);
  const unsafeRunIds = all.filter((id) => !isSafeRunId(id));
  const timeline: TimelineEntry[] = [
    { at: inc.occurred_at, text: 'The incident occurred (time declared when the record was opened).' },
  ];
  if (inc.aware_at) timeline.push({ at: inc.aware_at, text: 'The organisation became aware of it — the Art. 73 clock starts here.' });
  if (inc.created_at) {
    timeline.push({
      at: inc.created_at,
      text: `Record opened in NovaFabric: "${inc.title}" (${inc.severity}, ${inc.classification}), status open.`,
    });
  }
  for (const h of inc.history ?? []) {
    timeline.push({
      at: h.at,
      text: h.kind === 'status'
        ? `Status moved from ${h.from_value ?? 'none'} to ${h.to_value}.`
        : h.kind === 'attestation'
          ? `Attestation reference set to ${h.to_value}.`
          : `${h.kind}: ${h.to_value}.`,
    });
  }
  timeline.sort((a, b) => (Date.parse(a.at ?? '') || 0) - (Date.parse(b.at ?? '') || 0));
  if (inc.status === 'open' && inc.nearest_deadline) {
    const overdue = Date.parse(inc.nearest_deadline) < nowMs;
    const obligation = inc.nearest_obligation?.replace('art73_', 'Art. 73 ') ?? 'reporting obligation';
    timeline.push({
      at: inc.nearest_deadline,
      text: `${overdue ? 'Overdue' : 'Next'}: ${obligation} report due (operational aid, not legal advice).`,
    });
  }

  const commands: SafeCommand[] = [
    {
      name: 'nova incident status',
      flags: [],
      template: `nova incident status ${shellQuote(inc.id)}`,
      purpose: 'Show the full incident record and its deadline table. Read-only.',
    },
    ...runIds.flatMap(runCommands),
  ];
  if (runIds.length >= 2) commands.push(diffCommand(runIds[0], runIds[1]));
  commands.push({
    name: 'nova incident export',
    flags: ['--format'],
    template: `nova incident export ${shellQuote(inc.id)} --format aim`,
    purpose: 'Write an OECD AIM report JSON for this incident (incident-<id>-aim.json). Changes no record.',
  });

  return {
    kind: 'incident',
    title: inc.title,
    recordId: inc.id,
    runIds,
    noRunReason: all.length ? null : 'No run linked — the incident was opened without run ids.',
    noRunHint: all.length
      ? null
      : 'Incidents link runs only when opened: `nova incident open … --run-id <run>` (repeatable). The dashboard form does not set run ids yet.',
    claimedRun: null,
    failedCapsule: null,
    unsafeRunIds,
    verify: { kind: 'not-checked' },
    firstDivergence: null,
    timeline,
    commands,
  };
}
