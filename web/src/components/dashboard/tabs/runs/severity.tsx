/**
 * Severity badge styling shared by SecretScanPanel and ScanSecretsPanel.
 * Extracted verbatim from the former RunsTab monolith — behavior frozen.
 */

export const SEVERITY_STYLE: Record<string, string> = {
  critical: 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)]',
  high: 'text-[color-mix(in_oklab,var(--color-status-failure)_70%,var(--color-status-pending))] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)] border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]',
  medium: 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_10%,transparent)] border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]',
  low: 'text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] border-[var(--color-border)]',
  info: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)] border-[var(--color-border)]',
};

export function SeverityBadge({ severity }: { severity: string }) {
  const cls = SEVERITY_STYLE[severity] ?? SEVERITY_STYLE.info;
  return (
    <span className={`inline-block font-mono text-[var(--text-2xs)] uppercase px-1.5 py-px rounded border ${cls}`}>
      {severity}
    </span>
  );
}
