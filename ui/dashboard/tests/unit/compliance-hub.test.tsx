/**
 * Compliance hub: manifest integrity + group-switching render behavior.
 *
 * The hub itself calls api.listRuns on mount, so @/lib/api is stubbed. Panels
 * in the default (frameworks) and audits groups do not auto-load on mount;
 * the exports group would (AIBOMStatusPanel / GenericExportPanel), so the
 * render test deliberately switches frameworks → audits only.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '@/lib/ToastContext';
import {
  COMPLIANCE_GROUPS,
  COMPLIANCE_PANELS,
} from '@/components/dashboard/tabs/compliance';
import ComplianceTab from '@/components/dashboard/tabs/ComplianceTab';

vi.mock('@/lib/api', () => ({
  api: {
    listRuns: async () => ({ runs: [] }),
  },
  // useMutation imports ServeApiError from the same module.
  ServeApiError: class ServeApiError extends Error {},
}));

const VALID_GROUPS = ['frameworks', 'audits', 'privacy', 'exports', 'assurance'];

describe('compliance panel manifest', () => {
  it('has exactly 22 panels', () => {
    expect(COMPLIANCE_PANELS).toHaveLength(22);
  });

  it('panel ids are unique and non-empty', () => {
    const ids = COMPLIANCE_PANELS.map((p) => p.id);
    expect(ids.every((id) => id.length > 0)).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('panel titles are non-empty and components are defined', () => {
    for (const p of COMPLIANCE_PANELS) {
      expect(p.title.length).toBeGreaterThan(0);
      expect(p.component).toBeTruthy();
    }
  });

  it('every group value is one of the 5 valid groups', () => {
    for (const p of COMPLIANCE_PANELS) {
      expect(VALID_GROUPS).toContain(p.group);
    }
    for (const g of COMPLIANCE_GROUPS) {
      expect(VALID_GROUPS).toContain(g.value);
    }
  });

  it('every group is non-empty', () => {
    for (const g of COMPLIANCE_GROUPS) {
      const members = COMPLIANCE_PANELS.filter((p) => p.group === g.value);
      expect(members.length).toBeGreaterThan(0);
    }
  });

  it('declares all 5 groups exactly once', () => {
    expect(COMPLIANCE_GROUPS.map((g) => g.value)).toEqual(VALID_GROUPS);
  });
});

describe('ComplianceTab hub', () => {
  beforeEach(() => {
    // Reset any ?sub= left behind by a previous test.
    window.history.replaceState({}, '', '/');
  });

  function renderHub() {
    return render(
      <ToastProvider>
        <ComplianceTab />
      </ToastProvider>,
    );
  }

  it('renders the frameworks group by default and switches to audits', () => {
    renderHub();

    // Header + subnav.
    expect(screen.getByRole('heading', { name: 'Compliance' })).toBeInTheDocument();
    expect(screen.getByRole('tablist', { name: 'Compliance areas' })).toBeInTheDocument();

    // Default group: frameworks panels visible, audits panels not.
    expect(screen.getByText('EU AI Act Annex IV')).toBeInTheDocument();
    expect(screen.getByText('NIS2 Incident Report')).toBeInTheDocument();
    expect(screen.queryByText('Audit Coverage')).not.toBeInTheDocument();

    // Switch to audits.
    fireEvent.click(screen.getByRole('tab', { name: /Audits/ }));
    expect(screen.getByText('Audit Coverage')).toBeInTheDocument();
    expect(screen.getByText('Audit Bundle Export')).toBeInTheDocument();
    expect(screen.getByText('Audit Report Verify')).toBeInTheDocument();
    expect(screen.queryByText('EU AI Act Annex IV')).not.toBeInTheDocument();

    // Deep link written to the URL.
    expect(new URLSearchParams(window.location.search).get('sub')).toBe('audits');
  });

  it('normalizes an unknown ?sub= value back to frameworks', () => {
    window.history.replaceState({}, '', '/?sub=nonsense');
    renderHub();
    expect(screen.getByText('EU AI Act Annex IV')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Frameworks/ })).toHaveAttribute('aria-selected', 'true');
  });
});
