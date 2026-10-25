/** Review frontend-dashboard-05 — the dry-run pane counted "BLOCK", a word the report never contains. */
import { describe, expect, it } from 'vitest';

import { replayReportCounts } from '@/components/dashboard/tabs/runs/replayReport';

const EXACT_REPORT = `[dry-run: no execution]
Tool call policy report:
  [ALLOW] read_file  mutation_class=read  (read-only)
  [DENY] delete_db  mutation_class=destructive  (needs --allow-destructive)
  [DENY (label drift)] DENY  mutation_class=unknown  (unlabelled)
  [MOCK] send_email  mutation_class=external  (served from the capsule)
`;

describe('replayReportCounts', () => {
  it('counts the DENY decisions and every checked call of a real report', () => {
    expect(replayReportCounts(EXACT_REPORT)).toEqual({ checked: 4, denied: 2 });
  });

  it('does not count the word in a tool name or reason', () => {
    expect(replayReportCounts('  [ALLOW] DENY  mutation_class=read  (DENY nothing)\n')).toEqual({
      checked: 1,
      denied: 0,
    });
  });

  it('a capsule with no tool calls checked nothing', () => {
    expect(replayReportCounts('[dry-run] No tool calls in this capsule.\n')).toEqual({ checked: 0, denied: 0 });
    expect(replayReportCounts(undefined)).toEqual({ checked: 0, denied: 0 });
  });
});
