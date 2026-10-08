/**
 * Typed manifest of every compliance panel, grouped for the ComplianceTab
 * sub-navigation hub. Adding a panel = one file in this directory + one entry
 * here; the hub renders whatever the manifest declares.
 */
import type { ComponentType } from 'react';

import ToolPermissionPanel from './ToolPermissionPanel';
import AnnexIVPanel from './AnnexIVPanel';
import NIS2Panel from './NIS2Panel';
import SubjectProofPanel from './SubjectProofPanel';
import GdprErasurePanel from './GdprErasurePanel';
import AuditPanel from './AuditPanel';
import ExaminerPanel from './ExaminerPanel';
import AuditCoveragePanel from './AuditCoveragePanel';
import AuditBundlePanel from './AuditBundlePanel';
import AuditVerifyPanel from './AuditVerifyPanel';
import AuditMapPanel from './AuditMapPanel';
import AssurancePanel from './AssurancePanel';
import RoCrateExportPanel from './RoCrateExportPanel';
import C2paExportPanel from './C2paExportPanel';
import RoPAExportPanel from './RoPAExportPanel';
import AIBOMExportPanel from './AIBOMExportPanel';
import NISTRMFExportPanel from './NISTRMFExportPanel';
import AIBOMStatusPanel from './AIBOMStatusPanel';
import AibomGeneratePanel from './AibomGeneratePanel';
import PiiErasePanel from './PiiErasePanel';
import HIPAAProofPanel from './HIPAAProofPanel';
import GenericExportPanel from '../../compliance/GenericExportPanel';

import type { ComplianceGroup } from './groups';

export { COMPLIANCE_GROUPS, type ComplianceGroup } from './groups';

export interface CompliancePanelDef {
  id: string;
  title: string;
  group: ComplianceGroup;
  needsRunIds: boolean;
  component: ComponentType<{ runIds: string[] }> | ComponentType<Record<string, never>>;
}

/** Order within a group is the render order in the hub. */
export const COMPLIANCE_PANELS: CompliancePanelDef[] = [
  // -- frameworks --------------------------------------------------------
  { id: 'annex-iv', title: 'EU AI Act Annex IV', group: 'frameworks', needsRunIds: true, component: AnnexIVPanel },
  { id: 'nis2', title: 'NIS2 Incident Report', group: 'frameworks', needsRunIds: true, component: NIS2Panel },
  { id: 'nist-rmf-export', title: 'NIST AI RMF Report', group: 'frameworks', needsRunIds: true, component: NISTRMFExportPanel },
  { id: 'hipaa-proof', title: 'HIPAA Safe Harbor Proof', group: 'frameworks', needsRunIds: true, component: HIPAAProofPanel },
  // -- audits ------------------------------------------------------------
  { id: 'audit', title: 'Compliance Audit', group: 'audits', needsRunIds: true, component: AuditPanel },
  { id: 'audit-coverage', title: 'Audit Coverage', group: 'audits', needsRunIds: false, component: AuditCoveragePanel },
  { id: 'audit-bundle', title: 'Audit Bundle Export', group: 'audits', needsRunIds: false, component: AuditBundlePanel },
  { id: 'audit-verify', title: 'Audit Report Verify', group: 'audits', needsRunIds: false, component: AuditVerifyPanel },
  { id: 'audit-map', title: 'Audit Control Map', group: 'audits', needsRunIds: false, component: AuditMapPanel },
  // -- privacy -----------------------------------------------------------
  { id: 'subject-proof', title: 'GDPR Subject Proof', group: 'privacy', needsRunIds: false, component: SubjectProofPanel },
  { id: 'gdpr-erasure', title: 'GDPR Erasure Request', group: 'privacy', needsRunIds: true, component: GdprErasurePanel },
  { id: 'pii-erase', title: 'DEK Crypto-Shredding', group: 'privacy', needsRunIds: true, component: PiiErasePanel },
  { id: 'ropa-export', title: 'GDPR Art.30 RoPA Export', group: 'privacy', needsRunIds: true, component: RoPAExportPanel },
  // -- exports -----------------------------------------------------------
  { id: 'examiner', title: 'Examiner Export', group: 'exports', needsRunIds: true, component: ExaminerPanel },
  { id: 'ro-crate', title: 'RO-Crate Export', group: 'exports', needsRunIds: true, component: RoCrateExportPanel },
  { id: 'c2pa', title: 'C2PA Manifest Export', group: 'exports', needsRunIds: true, component: C2paExportPanel },
  { id: 'aibom-export', title: 'AI-SBOM Export', group: 'exports', needsRunIds: true, component: AIBOMExportPanel },
  { id: 'aibom-status', title: 'AI-SBOM Coverage Status', group: 'exports', needsRunIds: false, component: AIBOMStatusPanel },
  { id: 'aibom-generate', title: 'Generate AI-SBOM', group: 'exports', needsRunIds: true, component: AibomGeneratePanel },
  { id: 'generic-export', title: 'Generic Export', group: 'exports', needsRunIds: true, component: GenericExportPanel },
  // -- assurance ---------------------------------------------------------
  { id: 'tool-permission', title: 'Tool Permission Events', group: 'assurance', needsRunIds: true, component: ToolPermissionPanel },
  { id: 'assurance', title: 'OWASP LLM Assurance Check', group: 'assurance', needsRunIds: true, component: AssurancePanel },
];

