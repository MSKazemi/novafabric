/**
 * Filter/action row container — consistent spacing for the control strips
 * above tables and charts.
 */
import type { ReactNode } from 'react';
import { clsx } from 'clsx';

export interface ToolbarProps {
  /** Right-aligned cluster. */
  end?: ReactNode;
  className?: string;
  children?: ReactNode;
}

export default function Toolbar({ end, className, children }: ToolbarProps) {
  return (
    <div className={clsx('flex items-center gap-2 flex-wrap', className)}>
      {children}
      {end && <div className="ml-auto flex items-center gap-2 flex-wrap">{end}</div>}
    </div>
  );
}
