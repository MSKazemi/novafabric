/**
 * Counts from the dry-run policy report (`PolicyEvaluator.dry_run_report`).
 *
 * Each decision is one line `  [MOCK|ALLOW|LIVE|DENY] tool  mutation_class=…`, optionally
 * `[DENY (label note)]`. The report never says "BLOCK" — the pane counted that word and
 * so could not show a denied call (review frontend-dashboard-05). Only a line that
 * *starts* with the bracket counts, so a tool called `DENY` is not a decision.
 */
const DECISION_LINE = /^[ \t]*\[(MOCK|ALLOW|LIVE|DENY)\b/gm;

export interface ReplayReportCounts {
  /** Decisions the report lists, i.e. the tool calls it checked. */
  checked: number;
  /** Calls the policy refuses (`[DENY]`). */
  denied: number;
}

export function replayReportCounts(report: string | null | undefined): ReplayReportCounts {
  const counts: ReplayReportCounts = { checked: 0, denied: 0 };
  if (!report) return counts;
  for (const match of report.matchAll(DECISION_LINE)) {
    counts.checked += 1;
    if (match[1] === 'DENY') counts.denied += 1;
  }
  return counts;
}
