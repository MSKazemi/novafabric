import Button from '../ui/primitives/Button';
import StatusPill from '../ui/primitives/StatusPill';

export function StatusDot({ status }: { status: string | null }) {
  return <StatusPill status={status} variant="dot" />;
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[var(--color-danger-tint)] p-4 text-sm text-[var(--color-status-failure)]">
      <p>Error: {message}</p>
      {onRetry && (
        <Button variant="danger" size="sm" className="mt-2" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function Loading() {
  return <p className="p-6 text-sm text-[var(--color-text-muted)]">Loading…</p>;
}

export function SectionHeader({
  title, count, total, extra, action,
}: {
  title: string;
  count?: number;
  total?: number;
  extra?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 mb-4">
      <div className="flex items-baseline gap-2">
        <h2 className="text-sm font-medium text-[var(--color-text)]">{title}</h2>
        {count !== undefined && (
          <span className="text-xs font-mono text-[var(--color-text-faint)] tabular-nums">
            {count}{total !== undefined && total !== count ? ` / ${total}` : ''}
          </span>
        )}
        {extra && <span className="text-[10px] text-[var(--color-text-faint)] font-mono">{extra}</span>}
      </div>
      {action}
    </div>
  );
}
