/**
 * One measure value, honest about absence (ADR-0234 D2) and, for an
 * ADR-0236 ratio, always shown with the numerator and denominator it came
 * from — a rate without its operands cannot be checked.
 */
import type { DerivedColumn } from '../../../lib/dashboardTypes';
import { NO_VALUE, NO_VALUE_EXPLAINED, NO_VALUE_LABEL, formatValue, isAbsent, ratioParts } from './model';

export function NoValue({ reason }: { reason?: string | null }) {
  return (
    <span
      className="font-mono text-[var(--color-text-faint)]"
      title={reason ? `${NO_VALUE_EXPLAINED} (${reason})` : NO_VALUE_EXPLAINED}
      data-testid="no-value"
    >
      <span aria-hidden="true">{NO_VALUE}</span>
      <span className="sr-only">{reason ? `${NO_VALUE_LABEL}: ${reason}` : NO_VALUE_LABEL}</span>
    </span>
  );
}

export function RatioOperands({
  derived,
  row,
}: {
  derived: DerivedColumn;
  row: Record<string, unknown>;
}) {
  const parts = ratioParts(derived, row);
  const operand = (value: unknown) =>
    isAbsent(value) ? <NoValue /> : <span className="text-[var(--color-text-muted)]">{formatValue(value)}</span>;
  return (
    <span className="block text-2xs font-mono text-[var(--color-text-faint)]" data-testid="ratio-operands">
      {operand(parts.numerator.value)}
      <span aria-hidden="true"> ÷ </span>
      <span className="sr-only"> divided by </span>
      {operand(parts.denominator.value)}
      {parts.reason && <span className="ml-1">({parts.reason})</span>}
    </span>
  );
}

export default function ValueCell({
  value,
  unit,
  derived,
  row,
}: {
  value: unknown;
  unit?: string;
  derived?: DerivedColumn;
  row?: Record<string, unknown>;
}) {
  const reason = derived && row ? ratioParts(derived, row).reason : null;
  return (
    <span className="tabular-nums">
      {isAbsent(value) ? <NoValue reason={reason} /> : <span>{formatValue(value, unit)}</span>}
      {derived && row && <RatioOperands derived={derived} row={row} />}
    </span>
  );
}
