/**
 * Aligned A/B comparison of two runs' model and tool calls (issue #270, first slice).
 *
 * Pure read of the `GET /api/diff` response: every verdict here is the server's
 * (`DiffEngine`, the same code `nova diff` runs). Nothing is re-diffed in the
 * browser. The pairs keep the engine's alignment order, and the first divergent
 * step is the engine's own `first_divergence` (issue #74), matched to a pair by
 * its record ids — never guessed from a position.
 *
 * Where the response cannot say something, the result says so: a missing
 * `first_divergence` key (an older server) is `not_derivable`, and record lines
 * the engine skipped as malformed make the comparison `incomplete`, so a changed
 * or partial diff is never shown as "identical".
 */

export type StreamKey = 'model_calls' | 'tool_calls';
export type Verdict = 'changed' | 'added' | 'removed' | 'unchanged';

export interface AlignedRow {
  /** Stable DOM-safe key: `<stream>-<position>`. */
  key: string;
  stream: StreamKey;
  /** 0-based position of the pair in the engine's alignment for this stream. */
  position: number;
  verdict: Verdict;
  /** What differs, in the engine's words (`request`, `response`, `arguments`, …). */
  aspects: string[];
  /** Sampling parameters that changed (model calls only). */
  changedParams: string[];
  idA: string | null;
  idB: string | null;
  /** Tool name for tool calls; null for model calls (the server sends none). */
  toolName: string | null;
  statusA: string | null;
  statusB: string | null;
  /** `replay_placeholder_a/_b`: the record is a replay placeholder of this kind. */
  placeholderA: string | null;
  placeholderB: string | null;
  isFirstDivergence: boolean;
}

export interface FirstDivergenceDetail {
  /** Step index counted from 0, as `nova diff` prints it. */
  index: number;
  indexA: number | null;
  indexB: number | null;
  stream: StreamKey;
  eventType: string;
  reason: 'changed' | 'added' | 'removed';
  aspects: string[];
  recordIdA: string | null;
  recordIdB: string | null;
  /** `started_at`: model and tool calls merged by time; `per_stream`: one stream only. */
  timeline: 'started_at' | 'per_stream';
}

export type FirstDivergence =
  | { kind: 'found'; detail: FirstDivergenceDetail; rowKey: string | null }
  | { kind: 'none' }
  | { kind: 'not_derivable'; why: string };

export interface AlignedComparison {
  /** The ids the comparison was requested with (what `nova diff` takes). */
  runA: string;
  runB: string;
  rows: Record<StreamKey, AlignedRow[]>;
  firstDivergence: FirstDivergence;
  /** The server's gate verdict (`has_changes`); null when the response has none. */
  hasChanges: boolean;
  /** Record lines the engine skipped as malformed, both runs, both files. */
  skippedLines: number;
  environmentChanges: number;
  outputChanges: number;
}

type Obj = Record<string, unknown>;

const asObj = (v: unknown): Obj => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : {});
const asStr = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const asInt = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) ? v : null);
const asList = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/** Report flag -> aspect name, per stream, in the engine's order (`_first_divergence._ASPECTS`). */
const ASPECTS: Record<StreamKey, Array<[string, string]>> = {
  model_calls: [
    ['request_changed', 'request'],
    ['response_changed', 'response'],
    ['provider_changed', 'provider'],
    ['params_changed', 'params'],
  ],
  tool_calls: [
    ['arguments_changed', 'arguments'],
    ['result_changed', 'result'],
    ['status_changed', 'status'],
  ],
};

const ID_KEYS: Record<StreamKey, [string, string]> = {
  model_calls: ['model_call_id_a', 'model_call_id_b'],
  tool_calls: ['tool_call_id_a', 'tool_call_id_b'],
};

function verdictOf(pair: Obj): Verdict {
  // Same precedence as the engine's `_reason`.
  if (pair.added === true) return 'added';
  if (pair.removed === true) return 'removed';
  if (pair.changed === true) return 'changed';
  return 'unchanged';
}

function buildRows(stream: StreamKey, raw: unknown): AlignedRow[] {
  const [idKeyA, idKeyB] = ID_KEYS[stream];
  return asList(raw).map((item, position) => {
    const pair = asObj(item);
    const verdict = verdictOf(pair);
    return {
      key: `${stream}-${position}`,
      stream,
      position,
      verdict,
      aspects: verdict === 'changed' ? ASPECTS[stream].filter(([flag]) => pair[flag] === true).map(([, name]) => name) : [],
      changedParams: asList(pair.changed_params).filter((p): p is string => typeof p === 'string'),
      idA: asStr(pair[idKeyA]),
      idB: asStr(pair[idKeyB]),
      toolName: stream === 'tool_calls' ? asStr(pair.tool_name) : null,
      statusA: asStr(pair.status_a),
      statusB: asStr(pair.status_b),
      placeholderA: asStr(pair.replay_placeholder_a),
      placeholderB: asStr(pair.replay_placeholder_b),
      isFirstDivergence: false,
    };
  });
}

function parseDivergence(raw: unknown): FirstDivergenceDetail | null {
  const d = asObj(raw);
  const stream = d.stream;
  const reason = d.reason;
  const index = asInt(d.index);
  if ((stream !== 'model_calls' && stream !== 'tool_calls') || index === null) return null;
  if (reason !== 'changed' && reason !== 'added' && reason !== 'removed') return null;
  return {
    index,
    indexA: asInt(d.index_a),
    indexB: asInt(d.index_b),
    stream,
    eventType: asStr(d.event_type) ?? (stream === 'model_calls' ? 'model_call' : 'tool_call'),
    reason,
    aspects: asList(d.aspects).filter((a): a is string => typeof a === 'string'),
    recordIdA: asStr(d.record_id_a),
    recordIdB: asStr(d.record_id_b),
    timeline: d.timeline === 'started_at' ? 'started_at' : 'per_stream',
  };
}

/**
 * The one pair the divergence names: same stream, same verdict, same record id on
 * each side. With no record ids, or with ids shared by several pairs, the pair is
 * not identifiable and null is returned rather than a guess.
 */
function matchRow(rows: AlignedRow[], d: FirstDivergenceDetail): AlignedRow | null {
  if (d.recordIdA === null && d.recordIdB === null) return null;
  const hits = rows.filter(r => r.verdict === d.reason && r.idA === d.recordIdA && r.idB === d.recordIdB);
  return hits.length === 1 ? hits[0] : null;
}

function countSkipped(raw: unknown): number {
  let total = 0;
  for (const files of Object.values(asObj(raw))) {
    for (const n of Object.values(asObj(files))) {
      if (typeof n === 'number' && n > 0) total += n;
    }
  }
  return total;
}

/** Shape a `GET /api/diff` response (or a bare `nova diff --output-format json` report). */
export function buildAlignedComparison(raw: unknown, requested?: { runA?: string; runB?: string }): AlignedComparison {
  const wrapper = asObj(raw);
  // `/api/diff` nests the engine's report under `report`; the CLI's JSON is flat.
  const report = 'report' in wrapper ? asObj(wrapper.report) : wrapper;
  const sections = asObj(wrapper.sections ?? report.sections);

  const modelPairs = report.model_call_pairs ?? asObj(sections.model_calls).pairs;
  const toolPairs = report.tool_call_pairs ?? asObj(sections.tool_calls).pairs;
  const rows: Record<StreamKey, AlignedRow[]> = {
    model_calls: buildRows('model_calls', modelPairs),
    tool_calls: buildRows('tool_calls', toolPairs),
  };

  const environmentChanges = asList(report.env_changes ?? asObj(sections.environment).changes).length;
  const outputChanges = asList(report.output_changes ?? asObj(sections.outputs).changes).length;
  const anyRowDiffers = [...rows.model_calls, ...rows.tool_calls].some(r => r.verdict !== 'unchanged');
  // The server's verdict wins; without one, any listed difference counts. Never
  // "no changes" because a flag was missing.
  const serverVerdict = typeof wrapper.has_changes === 'boolean' ? wrapper.has_changes
    : typeof report.has_changes === 'boolean' ? report.has_changes : null;
  const hasChanges = serverVerdict === true || anyRowDiffers || environmentChanges > 0 || outputChanges > 0;

  const skippedLines = countSkipped(wrapper.skipped_malformed_lines ?? report.skipped_malformed_lines);

  let firstDivergence: FirstDivergence;
  const fdRaw = 'first_divergence' in report ? report.first_divergence
    : 'first_divergence' in wrapper ? wrapper.first_divergence : undefined;
  if (fdRaw === undefined) {
    firstDivergence = { kind: 'not_derivable', why: 'This server does not report a first divergence; upgrade nova serve, or run the CLI command below.' };
  } else if (fdRaw === null) {
    firstDivergence = anyRowDiffers
      // The engine found no divergent call but lists one: contradictory input.
      ? { kind: 'not_derivable', why: 'The server reports no first divergence but lists a changed, added or removed call.' }
      : { kind: 'none' };
  } else {
    const detail = parseDivergence(fdRaw);
    if (detail === null) {
      firstDivergence = { kind: 'not_derivable', why: 'The server sent a first divergence this dashboard cannot read.' };
    } else {
      const row = matchRow(rows[detail.stream], detail);
      if (row) row.isFirstDivergence = true;
      firstDivergence = { kind: 'found', detail, rowKey: row?.key ?? null };
    }
  }

  return {
    runA: requested?.runA || asStr(wrapper.run_a_id) || asStr(report.run_a_id) || '',
    runB: requested?.runB || asStr(wrapper.run_b_id) || asStr(report.run_b_id) || '',
    rows,
    firstDivergence,
    hasChanges,
    skippedLines,
    environmentChanges,
    outputChanges,
  };
}

/** POSIX-shell quoting: bare when safe, else `'…'` (same rule as the Commands tab). */
export function shellQuote(value: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(value) ? value : `'${value.replace(/'/g, `'\\''`)}'`;
}

/**
 * The equivalent CLI invocation. `nova diff` takes a run id or a capsule path for
 * each side (`cli/_capsule_ref.py`) and prints the same first divergence in its
 * text output; `--output-format json` gives the report this view reads.
 */
export function diffCliCommand(runA: string, runB: string, opts: { json?: boolean } = {}): string {
  const parts = ['nova', 'diff', shellQuote(runA), shellQuote(runB)];
  if (opts.json) parts.push('--output-format', 'json');
  return parts.join(' ');
}

const VERDICT_WORDS: Record<Verdict, string> = {
  changed: 'Changed',
  added: 'Added in B',
  removed: 'Removed in B',
  unchanged: 'Unchanged',
};

/** The verdict in words — the view never relies on colour alone. */
export function verdictLabel(verdict: Verdict): string {
  return VERDICT_WORDS[verdict];
}

/** "what differs" for one row, in words. */
export function describeRow(row: AlignedRow): string {
  if (row.verdict === 'added') return 'only in run B';
  if (row.verdict === 'removed') return 'only in run A';
  if (row.verdict === 'unchanged') return '—';
  const parts = row.aspects.map(a => {
    if (a === 'params' && row.changedParams.length > 0) return `params (${row.changedParams.join(', ')})`;
    if (a === 'status' && (row.statusA !== null || row.statusB !== null)) return `status ${row.statusA ?? '—'} → ${row.statusB ?? '—'}`;
    return a;
  });
  return parts.length > 0 ? parts.join(', ') : 'changed (no detail reported)';
}

/** One-line summary of the first divergence, worded like `nova diff`'s text output. */
export function describeDivergence(d: FirstDivergenceDetail): string {
  const what = d.eventType.replace('_', ' ');
  const aspects = d.aspects.length > 0 ? ` (${d.aspects.join(', ')})` : '';
  return `Step ${d.index}: ${what} ${d.reason}${aspects}`;
}

/** How the step index was counted, so a per-stream position is never read as a time order. */
export function describeTimeline(d: FirstDivergenceDetail): string {
  return d.timeline === 'started_at'
    ? 'Steps count model and tool calls together, ordered by started_at, from 0.'
    : `Steps count ${d.stream === 'model_calls' ? 'model' : 'tool'} calls only, from 0: some call has no started_at, so no order across model and tool calls is claimed.`;
}
