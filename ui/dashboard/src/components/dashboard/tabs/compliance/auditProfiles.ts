/**
 * The built-in compliance profile IDs the audit panels offer — one list, because four
 * copies diverged and the dashboard's defaults (`eu-ai-act`, `iso-42001`, `eu_ai_act`)
 * raised `ValueError` in the server's loader (review frontend-dashboard-08).
 *
 * Keep in step with `novafabric.compliance.audit.loader.list_profiles()`;
 * `tests/serve/test_dashboard_audit_profiles.py` fails when this list drifts.
 */
export const AUDIT_PROFILE_IDS = [
  'nist-ai-rmf',
  'eu-ai-act-high-risk',
  'gdpr',
  'soc2-type2',
  'iso42001',
  'scientific-reproducibility',
] as const;

export const DEFAULT_AUDIT_PROFILE: (typeof AUDIT_PROFILE_IDS)[number] = 'nist-ai-rmf';
