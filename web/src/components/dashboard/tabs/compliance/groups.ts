/**
 * Compliance group metadata, separated from the panel manifest so the
 * command palette (in the always-loaded DashboardApp chunk) can list groups
 * without pulling all 22 panel components into the main bundle.
 */
export type ComplianceGroup = 'frameworks' | 'audits' | 'privacy' | 'exports' | 'assurance';

export const COMPLIANCE_GROUPS: { value: ComplianceGroup; label: string }[] = [
  { value: 'frameworks', label: 'Frameworks' },
  { value: 'audits', label: 'Audits' },
  { value: 'privacy', label: 'Privacy' },
  { value: 'exports', label: 'Exports' },
  { value: 'assurance', label: 'Assurance' },
];
