// Shared status-badge vocabulary + tiny display helpers for the Infra tab
// panels. Extracted verbatim from InfraTab.tsx (dashboard-modernization split).

export type StatusBadge = 'shipped' | 'partial' | 'placeholder' | 'planned';

export const BADGE_LABEL: Record<StatusBadge, string> = {
  shipped: 'shipped',
  partial: 'partial UI',
  placeholder: 'CLI only',
  planned: 'planned',
};

export const BADGE_COLOR: Record<StatusBadge, string> = {
  shipped: 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)]',
  partial: 'text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)] border-[color-mix(in_oklab,var(--color-accent)_30%,transparent)]',
  placeholder: 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_12%,transparent)] border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]',
  planned: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)] border-[var(--color-border)]',
};

export function CmdBadge({ cmd }: { cmd: string }) {
  return (
    <code className="inline-block font-mono text-[10px] px-1.5 py-0.5 rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-text-muted)]">
      {cmd}
    </code>
  );
}

export function StatRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[10px] text-[var(--color-text-faint)] shrink-0">{label}:</span>
      <span className="text-[11px] text-[var(--color-text-muted)] truncate">{value}</span>
    </div>
  );
}
