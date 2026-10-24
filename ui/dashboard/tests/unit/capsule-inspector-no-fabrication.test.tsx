/**
 * Review frontend-dashboard-03 — the run inspector showed a fabricated
 * `redaction-proof.json` ("findings": [], a fixed chain hash) and a fabricated
 * `env.lock` for every real run, whatever the capsule actually held.
 *
 * Live runs now show the capsule's own redaction proof (or say it is absent) and say
 * that env.lock is not served; the sample content stays only on the showcase page,
 * labelled as a sample.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const getRedactionProof = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    getRedactionProof: (...a: unknown[]) => getRedactionProof(...a),
    listRuns: vi.fn(async () => ({ runs: [] })),
    getRunFile: vi.fn(),
  },
}));

import CapsuleInspector from '@/components/capsule/CapsuleInspector';

const live = {
  capsule: { run_id: '01LIVE00000000000000000001', schema_version: '0.1.0' },
  capsuleYaml: '',
  modelCalls: [],
  toolCalls: [],
  trace: [],
};

describe('CapsuleInspector', () => {
  beforeEach(() => getRedactionProof.mockReset());

  it("shows the live run's own redaction proof, with its findings", async () => {
    getRedactionProof.mockResolvedValue({ findings: [{ rule: 'openai.api_key' }], chain_hash: 'sha256:real' });
    render(<CapsuleInspector capsuleData={live} />);
    fireEvent.click(screen.getByRole('button', { name: /redaction-proof\.json/ }));
    await waitFor(() => expect(screen.getByText(/openai\.api_key/)).toBeTruthy());
    expect(getRedactionProof).toHaveBeenCalledWith('01LIVE00000000000000000001');
    expect(getRedactionProof).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/a3f8c4e9b1d6/)).toBeNull();
  });

  it('says so when the live run has no redaction proof', async () => {
    class NotFound extends Error {
      status = 404;
    }
    getRedactionProof.mockRejectedValueOnce(new NotFound('redaction proof not found'));
    render(<CapsuleInspector capsuleData={live} />);
    fireEvent.click(screen.getByRole('button', { name: /redaction-proof\.json/ }));
    await waitFor(() => expect(screen.getByText(/no redaction-proof\.json/i)).toBeTruthy());
    expect(screen.queryByText(/"findings": \[\]/)).toBeNull();
  });

  it('never shows a made-up env.lock for a live run', () => {
    render(<CapsuleInspector capsuleData={live} />);
    fireEvent.click(screen.getByRole('button', { name: /env\.lock/ }));
    expect(screen.queryByText(/Linux 6\.14\.0/)).toBeNull();
    expect(screen.getByText(/not served by the dashboard/i)).toBeTruthy();
  });

  it('keeps the sample on the showcase page, labelled as a sample', () => {
    render(<CapsuleInspector />);
    fireEvent.click(screen.getByRole('button', { name: /env\.lock/ }));
    expect(screen.getByText(/showcase sample/i)).toBeTruthy();
  });
});
