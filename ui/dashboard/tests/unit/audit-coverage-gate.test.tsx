/**
 * Audit Coverage panel: the verdict follows `gate_passed`, not `threshold_met`.
 *
 * `threshold_met` only says "score >= threshold". The route also returns `gate_passed`
 * (score >= threshold AND no finding) and `findings`; a capsule whose `.seal` fails
 * verification is a `seal_invalid` finding that must turn the verdict red and be shown,
 * even when the score clears the threshold. An older server that sends neither field
 * keeps the old behaviour (fallback to `threshold_met`).
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const auditCoverage = vi.fn();

vi.mock('@/lib/api', () => ({
  api: { auditCoverage: (...a: unknown[]) => auditCoverage(...a) },
  ServeApiError: class ServeApiError extends Error {},
}));

import AuditCoveragePanel from '@/components/dashboard/tabs/compliance/AuditCoveragePanel';

const base = {
  profile: 'nist-ai-rmf',
  overall_score: 0.92,
  threshold: 0.8,
  capsule_count: 3,
  total_controls: 10,
  covered_controls: 9,
  partial_controls: 1,
  missing_controls: 0,
  coverages: [],
};

async function runPanel() {
  render(<AuditCoveragePanel />);
  fireEvent.click(screen.getByText('Check coverage'));
  await waitFor(() => expect(screen.getByText('92.0%')).toBeTruthy());
  return screen.getByText('92.0%');
}

describe('AuditCoveragePanel gate verdict', () => {
  beforeEach(() => auditCoverage.mockReset());

  it('is red and lists the finding when gate_passed is false although threshold_met is true', async () => {
    auditCoverage.mockResolvedValue({
      ...base,
      threshold_met: true,
      gate_passed: false,
      findings: [
        { code: 'seal_invalid', capsule_id: 'cap-7', message: 'the .seal failed verification' },
      ],
    });
    const score = await runPanel();
    expect(score.className).toContain('status-failure');
    expect(score.className).not.toContain('status-success');
    expect(screen.getByText(/seal_invalid/)).toBeTruthy();
    expect(screen.getByText(/cap-7/)).toBeTruthy();
    expect(screen.getByText(/the \.seal failed verification/)).toBeTruthy();
    expect(screen.getByText(/gate failed/i)).toBeTruthy();
  });

  it('is green with no finding line when gate_passed is true', async () => {
    auditCoverage.mockResolvedValue({ ...base, threshold_met: true, gate_passed: true, findings: [] });
    const score = await runPanel();
    expect(score.className).toContain('status-success');
    expect(screen.queryByText(/seal_invalid/)).toBeNull();
    expect(screen.getByText(/gate passed/i)).toBeTruthy();
  });

  it('falls back to threshold_met when the server sends no gate_passed', async () => {
    auditCoverage.mockResolvedValue({ ...base, threshold_met: false });
    const score = await runPanel();
    expect(score.className).toContain('status-failure');
    expect(screen.queryByText(/seal_invalid/)).toBeNull();
  });
});
