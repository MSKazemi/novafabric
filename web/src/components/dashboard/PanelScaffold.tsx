/**
 * Shared scaffold for the repeated compliance-panel state machine.
 *
 * Owns the chrome every action panel repeats by hand: Card with title +
 * subtitle + capability/ADR badge, a form region, a pending-aware submit
 * button, the mono CLI-preview line, an error box, and a result region
 * (children). Pair with `useMutation`/`useQuery` for the async state itself —
 * see the Audit Coverage / Bundle / Verify panels for the exemplar pattern.
 */
import type { ReactNode } from 'react';
import { Badge, Button, Card } from '../ui/primitives';
import { ErrorBox } from './helpers';

export interface PanelScaffoldProps {
  /** Stable panel id (used as a DOM anchor: `panel-<id>`). */
  id: string;
  title: string;
  subtitle?: ReactNode;
  /** Small capability/ADR chip shown in the card header (e.g. "cap-002"). */
  capBadge?: string;
  /** CLI-equivalent command, rendered as a mono `$ …` preview line. */
  cli?: string;
  /** Form controls rendered above the submit button. */
  form?: ReactNode;
  onSubmit?: () => void;
  submitLabel?: string;
  submitDisabled?: boolean;
  pending?: boolean;
  error?: string | null;
  /** Result region rendered under the form/CLI/error chrome. */
  children?: ReactNode;
}

export default function PanelScaffold({
  id,
  title,
  subtitle,
  capBadge,
  cli,
  form,
  onSubmit,
  submitLabel,
  submitDisabled,
  pending,
  error,
  children,
}: PanelScaffoldProps) {
  return (
    <Card
      title={title}
      actions={capBadge ? <Badge tone="neutral">{capBadge}</Badge> : undefined}
    >
      <div id={`panel-${id}`} className="space-y-3">
        {subtitle && <p className="text-[10px] text-[var(--color-text-faint)]">{subtitle}</p>}
        {form}
        {onSubmit && (
          <Button
            variant="primary"
            onClick={onSubmit}
            pending={pending}
            disabled={submitDisabled}
          >
            {submitLabel ?? 'Run'}
          </Button>
        )}
        {cli && (
          <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] overflow-x-auto whitespace-nowrap">
            $ {cli}
          </div>
        )}
        {error && <ErrorBox message={error} />}
        {children}
      </div>
    </Card>
  );
}
