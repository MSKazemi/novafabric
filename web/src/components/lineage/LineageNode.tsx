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
        dimmed && 'opacity-25',
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
            <div className="truncate text-sm font-medium text-[var(--color-text)]" title={data.label}>
              {data.label}
            </div>
          </div>
          {data.subLabel && (
            <div className="truncate text-xs text-[var(--color-text-faint)] font-mono" title={data.subLabel}>
              {data.subLabel}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
