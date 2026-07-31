// Risk tier colour coding + vocabulary options shared by the Governance
// classification panels. Extracted verbatim from GovernanceTab.tsx
// (dashboard-modernization split).

export const EU_TIER_CONFIG: Record<string, { label: string; colorClass: string }> = {
  prohibited:   { label: 'Prohibited',    colorClass: 'text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]' },
  high_risk:    { label: 'High Risk',     colorClass: 'text-[var(--color-status-pending)] border-[color-mix(in_oklab,var(--color-status-pending)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)]' },
  limited_risk: { label: 'Limited Risk',  colorClass: 'text-[var(--color-accent)] border-[color-mix(in_oklab,var(--color-accent)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]' },
  minimal_risk: { label: 'Minimal Risk',  colorClass: 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]' },
};

export const NIST_IMPACT_CONFIG: Record<string, { label: string; colorClass: string }> = {
  critical: { label: 'Critical', colorClass: 'text-[var(--color-status-failure)]' },
  high:     { label: 'High',     colorClass: 'text-[var(--color-status-pending)]' },
  medium:   { label: 'Medium',   colorClass: 'text-[var(--color-accent)]' },
  low:      { label: 'Low',      colorClass: 'text-[var(--color-status-success)]' },
};

export const VOCAB_OPTIONS = [
  'eu-ai-act/2024.1.0',
  'nist-ai-rmf/1.0.0',
  'omb-m-24-10/1.0.0',
];
