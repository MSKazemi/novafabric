/**
 * AggregateRefusal — the one way the dashboard renders an ADR-0234 D2 refusal.
 *
 * A refused aggregate shows **why** and **what would make it computable**
 * (D3), never a number: "a number on screen is read as a number regardless of
 * the badge beside it". Used by the Runs aggregate strip, Analytics, Home and
 * Reports so the rule reads the same everywhere.
 */
import type { AggregateVerdict } from '../../lib/api';

const CONDITION_LABEL: Record<string, string> = {
  source_unavailable: 'source unavailable',
  tenant_unsafe_store: 'not tenant-safe',
  truncated_source: 'partial data',
  unpushable_filter: 'filter not applicable',
  absent_contributor: 'missing inputs',
};

export function conditionLabel(verdict: AggregateVerdict): string {
  return CONDITION_LABEL[verdict.condition ?? ''] ?? 'not computable';
}

export default function AggregateRefusal({
  verdict,
  compact = false,
  testId = 'aggregate-refusal',
}: {
  verdict: AggregateVerdict;
  compact?: boolean;
  testId?: string;
}) {
  if (verdict.computable) return null;
  return (
    <div
      role="status"
      data-testid={testId}
      data-condition={verdict.condition ?? ''}
      className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_45%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_7%,transparent)] px-2 py-1 text-[11px]"
    >
      <span className="font-mono uppercase tracking-wider text-[10px] text-[var(--color-status-pending)]">
        {conditionLabel(verdict)}
      </span>
      <span className="ml-2 text-[var(--color-text)]">{verdict.reason}</span>
      {!compact && verdict.remedy && (
        <p className="mt-0.5 text-[var(--color-text-muted)]">→ {verdict.remedy}</p>
      )}
      {compact && verdict.remedy && (
        <span className="ml-1 text-[var(--color-text-muted)]">— {verdict.remedy}</span>
      )}
    </div>
  );
}
