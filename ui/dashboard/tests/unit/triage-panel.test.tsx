/**
 * Issue #276 — alert/incident triage panel. From one alert or incident: the exact run(s) the
 * record links to, a timeline in words, and copyable next safe actions. Nothing is executed,
 * no run is guessed, and a run the server refuses is reported, never filled in.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { GENERATED_COMMANDS } from '@/components/dashboard/commands/generatedCommands';
import TriagePanel, { MAX_LOADED_RUNS, diffHref, formatAt, runHref } from '@/components/dashboard/triage/TriagePanel';
import { alertTriage, incidentTriage, isSafeRunId, renderCommand } from '@/components/dashboard/triage/triageModel';
import { ServeApiError, type AlertRow, type FullCapsule, type IncidentView } from '@/lib/api';

const sealAlert: AlertRow = {
  id: 'EVT-SEAL',
  timestamp: '2026-11-20T10:00:00+00:00',
  event_type: 'ops.seal.verify_failed',
  severity: 'critical',
  subject: 'capsule:3f2a',
  outcome: 'delivered',
  endpoint_id: 'webhook-1',
  attempts: 2,
  capsule_dir: '/data/capsules/3f2a',
  claimed_run_id: 'run-1',
  first_divergence: { path: 'model-calls.jsonl', reason: 'modified', stream: 'model_calls', divergent_files: 3 },
};

const quotaAlert: AlertRow = {
  id: 'E-Q',
  timestamp: '2026-11-20T09:00:00.000000Z',
  event_type: 'ops.quota.breached',
  severity: 'critical',
  subject: 'quota:capsules',
  outcome: 'emitted',
  endpoint_id: null,
  attempts: 0,
};

const incident: IncidentView = {
  id: 'inc-abc',
  title: 'Wrong refund approved',
  classification: 'financial_harm',
  severity: 'high',
  status: 'reported',
  occurred_at: '2026-11-01T08:00:00+00:00',
  aware_at: '2026-11-01T09:00:00+00:00',
  created_at: '2026-11-01T09:30:00+00:00',
  history: [{ kind: 'status', from_value: 'open', to_value: 'reported', at: '2026-11-02T12:00:00+00:00' }],
  run_ids: ['run-a', 'run-b'],
  deadlines: [],
};

function capsule(runId: string, extra: Partial<FullCapsule> = {}): FullCapsule {
  return {
    run_id: runId,
    capsule_path: `/data/capsules/${runId}`,
    manifest: { run_id: runId, status: 'success', created_at: '2026-11-01T07:59:00+00:00' },
    trace: [], model_calls: [], tool_calls: [], lineage: [], assets: [], inputs: [], outputs: [],
    ...extra,
  };
}

const catalog = new Map(GENERATED_COMMANDS.map((c) => [c.name, c]));

describe('triage model', () => {
  it("labels a seal alert's manifest run id as an unverified claim, never the subject", () => {
    const m = alertTriage(sealAlert);
    // A tampered capsule can name an innocent run: it is not loaded as a linked run.
    expect(m.runIds).toEqual([]);
    expect(m.claimedRun?.runId).toBe('run-1');
    expect(m.claimedRun?.caveat).toMatch(/does not vouch for/);
    expect(m.noRunReason).toBeNull();
    expect(m.failedCapsule).toEqual({ capsuleId: '3f2a', dir: '/data/capsules/3f2a' });
    expect(m.timeline.some((t) => /names run run-1 — unverified/.test(t.text))).toBe(true);
    expect(m.timeline.some((t) => /source recorded run/.test(t.text))).toBe(false);
  });

  it('never offers a command against the claimed run — only the capsule that failed', () => {
    const lines = alertTriage(sealAlert).commands.map((c) => renderCommand(c, null));
    expect(lines).toContain('nova verify /data/capsules/3f2a --json');
    expect(lines.some((l) => l.includes('run-1'))).toBe(false);
    const older = alertTriage({ ...sealAlert, capsule_dir: null, claimed_run_id: null });
    expect(older.commands.map((c) => renderCommand(c, null))).toContain(
      'nova verify <capsule-dir-of-capsule:3f2a> --json',
    );
  });

  it('rejects run ids that are only dots or contain a slash', () => {
    for (const bad of ['.', '..', '...', 'a/b', '../x', '', 'two words']) expect(isSafeRunId(bad)).toBe(false);
    for (const ok of ['run-1', 'r.1', '01HZ:abc@x']) expect(isSafeRunId(ok)).toBe(true);
    const m = alertTriage({ ...sealAlert, claimed_run_id: '..' });
    expect(m.claimedRun).toBeNull();
    expect(m.unsafeRunIds).toEqual(['..']);
    const inc = incidentTriage({ ...incident, run_ids: ['run-a', '..', 'a/b'] });
    expect(inc.runIds).toEqual(['run-a']);
    expect(inc.unsafeRunIds).toEqual(['..', 'a/b']);
  });

  it('says no run is linked when the alert recorded none', () => {
    const unlinked = alertTriage({ ...sealAlert, claimed_run_id: null, capsule_dir: null, first_divergence: null });
    expect(unlinked.runIds).toEqual([]);
    expect(unlinked.noRunReason).toBe('No run linked — the alert source did not record a run id.');
    // The subject's capsule id is a seal digest, never promoted to a run id.
    expect(unlinked.noRunHint).toMatch(/not a run id/);
    // The failed capsule is still re-verifiable by hand, via a visible placeholder.
    expect(unlinked.commands.find((c) => c.name === 'nova verify')?.template).toMatch(/<capsule-dir-of-capsule:3f2a>/);
  });

  it('treats a field missing on an older server like an unrecorded run', () => {
    const m = alertTriage(quotaAlert);
    expect(m.runIds).toEqual([]);
    expect(m.noRunReason).toMatch(/No run linked/);
    expect(m.verify).toEqual({ kind: 'not-checked' });
  });

  it('records a seal alert as a recorded failure, not a fresh check', () => {
    expect(alertTriage(sealAlert).verify).toEqual({
      kind: 'failed-recorded', at: sealAlert.timestamp, source: 'nova verify',
    });
  });

  it('only offers commands and flags the real CLI has', () => {
    const models = [alertTriage(sealAlert), alertTriage(quotaAlert), incidentTriage(incident)];
    const offenders: string[] = [];
    for (const m of models) {
      for (const c of m.commands) {
        const real = catalog.get(c.name);
        if (!real) { offenders.push(c.name); continue; }
        const flags = new Set(real.fields.filter((f) => !f.positional).map((f) => f.flag ?? `--${f.key}`));
        for (const f of c.flags) if (!flags.has(f)) offenders.push(`${c.name} ${f}`);
        // Every flag in the rendered line is declared, so the catalog check covers it.
        const used = renderCommand(c, '/x').split(' ').filter((t) => t.startsWith('-'));
        for (const f of used) if (!c.flags.includes(f)) offenders.push(`${c.name} undeclared ${f}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('shell-quotes values and shows a visible placeholder until the capsule dir is known', () => {
    const verify = incidentTriage(incident).commands.find((c) => c.name === 'nova verify')!;
    expect(renderCommand(verify, null)).toBe('nova verify <capsule-dir-of-run-a> --json');
    expect(renderCommand(verify, '/data/my capsules/run-a')).toBe("nova verify '/data/my capsules/run-a' --json");
    const seal = alertTriage({ ...sealAlert, capsule_dir: '/data/my capsules/3f2a' });
    expect(seal.commands.map((c) => renderCommand(c, null))).toContain("nova verify '/data/my capsules/3f2a' --json");
  });

  it('builds an incident timeline in time order and offers a diff for two linked runs', () => {
    const m = incidentTriage(incident);
    expect(m.timeline.map((t) => t.at)).toEqual([
      incident.occurred_at, incident.aware_at, incident.created_at, incident.history![0].at,
    ]);
    expect(m.timeline[3].text).toBe('Status moved from open to reported.');
    expect(m.commands.map((c) => renderCommand(c, null))).toContain('nova diff run-a run-b --json');
  });

  it('says plainly when an incident links no run', () => {
    const m = incidentTriage({ ...incident, run_ids: [] });
    expect(m.noRunReason).toBe('No run linked — the incident was opened without run ids.');
    expect(m.noRunHint).toMatch(/--run-id/);
  });
});

describe('TriagePanel', () => {
  it('shows the failed capsule, the claimed run as unverified, and loads no run for an alert', () => {
    const loadRun = vi.fn(async (id: string) => capsule(id));
    render(<TriagePanel model={alertTriage(sealAlert)} onClose={() => {}} loadRun={loadRun} />);
    expect(screen.getByRole('dialog', { name: /Triage — ops.seal.verify_failed/ })).toBeTruthy();
    expect(loadRun).not.toHaveBeenCalled(); // the claimed run is never loaded as the subject
    expect(screen.getByText('/data/capsules/3f2a')).toBeTruthy();
    const claim = screen.getByRole('note', { name: 'Unverified run claim' });
    expect(within(claim).getByText('unverified')).toBeTruthy();
    expect(claim.textContent).toMatch(/does not vouch for/);
    expect(within(claim).getByRole('link', { name: /Open claimed run run-1 \(unverified\)/ }).getAttribute('href'))
      .toMatch(/\?tab=runs&run=run-1$/);
    expect(screen.getByText('seal failed')).toBeTruthy();
    expect(screen.getByText('model-calls.jsonl')).toBeTruthy();
    expect(screen.getByText('nova verify /data/capsules/3f2a --json')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy nova verify /data/capsules/3f2a --json' })).toBeTruthy();
  });

  it('puts a non-plain run id into no request and no link', () => {
    const loadRun = vi.fn(async (id: string) => capsule(id));
    render(<TriagePanel model={incidentTriage({ ...incident, run_ids: ['..', 'run-a'] })} onClose={() => {}} loadRun={loadRun} />);
    expect(loadRun).toHaveBeenCalledTimes(1);
    expect(loadRun).toHaveBeenCalledWith('run-a');
    expect(screen.getByText(/Not a plain run id/)).toBeTruthy();
    for (const a of screen.getAllByRole('link')) expect(a.getAttribute('href')).not.toMatch(/run=\.\./);
  });

  it('shows an incident run with its capsule path and run-scoped commands', async () => {
    const loadRun = vi.fn(async (id: string) => capsule(id));
    render(<TriagePanel model={incidentTriage({ ...incident, run_ids: ['run-a'] })} onClose={() => {}} loadRun={loadRun} />);
    await waitFor(() => expect(screen.getByText('/data/capsules/run-a')).toBeTruthy());
    expect(screen.getByText('nova verify /data/capsules/run-a --json')).toBeTruthy();
    const open = screen.getByRole('link', { name: 'Open run run-a in the Runs tab' });
    expect(open.getAttribute('href')).toMatch(/\?tab=runs&run=run-a$/);
  });

  it('says "no run linked" and loads nothing when the alert recorded no run', () => {
    const loadRun = vi.fn();
    render(<TriagePanel model={alertTriage(quotaAlert)} onClose={() => {}} loadRun={loadRun} />);
    expect(screen.getByRole('note').textContent).toMatch('No run linked — the alert source did not record a run id.');
    expect(loadRun).not.toHaveBeenCalled();
    expect(screen.queryByRole('link', { name: /Open run/ })).toBeNull();
    expect(screen.getByText('not checked here')).toBeTruthy();
  });

  it('reports a refused run with the server message and invents no status or path', async () => {
    const loadRun = vi.fn(async () => { throw new ServeApiError(403, 'forbidden for this workspace'); });
    render(<TriagePanel model={incidentTriage({ ...incident, run_ids: ['run-a'] })} onClose={() => {}} loadRun={loadRun} />);
    await waitFor(() => expect(screen.getByText(/HTTP 403/)).toBeTruthy());
    expect(screen.getByText(/forbidden for this workspace/)).toBeTruthy();
    expect(screen.queryByText('success')).toBeNull();
    // The command keeps its placeholder rather than a guessed directory.
    expect(screen.getByText('nova verify <capsule-dir-of-run-a> --json')).toBeTruthy();
  });

  it('flags a capsule that is missing on disk', async () => {
    const loadRun = vi.fn(async (id: string) => capsule(id, { capsule_available: false }));
    render(<TriagePanel model={incidentTriage({ ...incident, run_ids: ['run-a'] })} onClose={() => {}} loadRun={loadRun} />);
    await waitFor(() => expect(screen.getByText(/missing on disk/)).toBeTruthy());
  });

  it('links two incident runs to Diff and caps how many runs it loads', async () => {
    const many = Array.from({ length: MAX_LOADED_RUNS + 2 }, (_, i) => `run-${i}`);
    const loadRun = vi.fn(async (id: string) => capsule(id));
    render(<TriagePanel model={incidentTriage({ ...incident, run_ids: many })} onClose={() => {}} loadRun={loadRun} />);
    await waitFor(() => expect(loadRun).toHaveBeenCalledTimes(MAX_LOADED_RUNS));
    expect(screen.getByText(/2 more linked runs not loaded here/)).toBeTruthy();
    const diff = screen.getByRole('link', { name: 'Compare run-0 and run-1 in Diff' });
    expect(diff.getAttribute('href')).toMatch(/\?run_ids=run-0%2Crun-1$/);
  });

  it('renders the timeline as an ordered list with machine-readable times', () => {
    render(<TriagePanel model={incidentTriage({ ...incident, run_ids: [] })} onClose={() => {}} loadRun={vi.fn()} />);
    const section = screen.getByRole('region', { name: 'What happened' });
    const items = within(section).getAllByRole('listitem');
    expect(items).toHaveLength(4);
    expect(items[0].querySelector('time')?.getAttribute('datetime')).toBe(incident.occurred_at);
  });

  it('closes on Escape', () => {
    const onClose = vi.fn();
    render(<TriagePanel model={alertTriage(quotaAlert)} onClose={onClose} loadRun={vi.fn()} />);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});

describe('link and time helpers', () => {
  it('builds same-page deep links', () => {
    expect(runHref('r 1')).toMatch(/\?tab=runs&run=r\+1$/);
    expect(diffHref(['a', 'b'])).toMatch(/\?run_ids=a%2Cb$/);
  });

  it('prints times in UTC and leaves an unparsable value as is', () => {
    expect(formatAt('2026-11-20T10:00:00+01:00')).toBe('2026-11-20 09:00:00 UTC');
    expect(formatAt('not a time')).toBe('not a time');
  });
});
