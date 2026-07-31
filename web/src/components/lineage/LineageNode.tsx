import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import type { LineageNodeData } from '../../lib/lineage';
import { clsx } from 'clsx';

const KIND_STYLE: Record<LineageNodeData['kind'], string> = {
  run: 'rounded-md',
  asset: 'rounded-full',
  artifact: 'rounded-sm',
};

const KIND_ICON: Record<LineageNodeData['kind'], string> = {
  run: 'R',
  asset: 'A',
  artifact: 'F',
};

const STATUS_DOT: Record<NonNullable<LineageNodeData['status']>, string> = {
  promoted: 'bg-[var(--color-status-success)]',
  development: 'bg-[var(--color-status-pending)]',
  success: 'bg-[var(--color-status-success)]',
  failure: 'bg-[var(--color-status-failure)]',
};

export default function LineageNodeRenderer(props: NodeProps<Node<LineageNodeData & { dimmed?: boolean; selected?: boolean }>>) {
  const { data } = props;
  const dimmed = (data.dimmed as boolean | undefined) ?? false;
  const isSelected = (data.selected as boolean | undefined) ?? false;

  return (
    <div
      className={clsx(
        'relative px-3 py-2 border bg-[var(--color-bg-raised)] transition-opacity',
        KIND_STYLE[data.kind],
        isSelected ? 'border-[var(--color-accent)] ring-2 ring-[var(--color-accent)]/30' : 'border-[var(--color-border)]',
        // De-emphasis must stay READABLE. Container opacity blends the label
        // toward the canvas, so ANY value below 100% degrades text contrast —
        // opacity-25 measured 1.26:1 where WCAG AA needs 4.5:1 (axe serious).
        // A node outside the current path is secondary, not decorative, so the
        // cue is now structural: recede the surface and border, keep the text
        // at full strength. `dimmed` also drives the label colors below.
        dimmed && 'bg-[var(--color-bg-sunken)] border-dashed',
      )}
      style={{ width: 220, height: 64 }}
    >
      <Handle type="target" position={Position.Top} className="!w-2 !h-2 !bg-[var(--color-border-strong)] !border-0" />
      <Handle type="source" position={Position.Bottom} className="!w-2 !h-2 !bg-[var(--color-border-strong)] !border-0" />

      <div className="flex items-center gap-2 h-full">
        <div
          className={clsx(
            'shrink-0 w-7 h-7 flex items-center justify-center font-mono text-xs font-semibold',
            KIND_STYLE[data.kind],
            'bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-text-muted)]',
          )}
          aria-hidden="true"
        >
          {KIND_ICON[data.kind]}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            {data.status && (
              <span className={clsx('w-1.5 h-1.5 rounded-full', STATUS_DOT[data.status])} aria-label={data.status} />
            )}
            <div
              className={clsx(
                'truncate text-sm font-medium',
                dimmed ? 'text-[var(--color-text-muted)]' : 'text-[var(--color-text)]',
              )}
              title={data.label}
            >
              {data.label}
            </div>
          </div>
          {data.subLabel && (
            <div
              className={clsx(
                'truncate text-xs font-mono',
                // Never push the sub-label below AA: faint-on-sunken already
                // sits near the floor, so a dimmed node keeps the muted tone.
                dimmed ? 'text-[var(--color-text-muted)]' : 'text-[var(--color-text-faint)]',
              )}
              title={data.subLabel}
            >
              {data.subLabel}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
