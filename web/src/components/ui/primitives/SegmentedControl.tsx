/**
 * Segmented control for sub-navigation inside a tab (?sub= deep links) and
 * mode switches. Roving tabindex + arrow-key navigation per WAI-ARIA tabs.
 */
import { useRef, type KeyboardEvent } from 'react';
import { clsx } from 'clsx';

export interface Segment<T extends string = string> {
  value: T;
  label: string;
  /** Small trailing count/annotation. */
  meta?: string | number;
}

export interface SegmentedControlProps<T extends string = string> {
  segments: readonly Segment<T>[];
  value: T;
  onChange: (value: T) => void;
  'aria-label': string;
  className?: string;
}

export default function SegmentedControl<T extends string = string>({
  segments,
  value,
  onChange,
  className,
  ...aria
}: SegmentedControlProps<T>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const onKeyDown = (e: KeyboardEvent, idx: number) => {
    let next: number | null = null;
    if (e.key === 'ArrowRight') next = (idx + 1) % segments.length;
    else if (e.key === 'ArrowLeft') next = (idx - 1 + segments.length) % segments.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = segments.length - 1;
    if (next !== null) {
      e.preventDefault();
      const seg = segments[next]!;
      onChange(seg.value);
      refs.current[next]?.focus();
    }
  };

  return (
    <div
      role="tablist"
      aria-label={aria['aria-label']}
      className={clsx(
        'inline-flex items-center gap-0.5 p-0.5 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-sunken)] overflow-x-auto max-w-full',
        className,
      )}
    >
      {segments.map((seg, idx) => {
        const active = seg.value === value;
        return (
          <button
            key={seg.value}
            ref={(el) => { refs.current[idx] = el; }}
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(seg.value)}
            onKeyDown={(e) => onKeyDown(e, idx)}
            className={clsx(
              'px-2.5 h-6 rounded text-2xs font-medium whitespace-nowrap transition-colors duration-[var(--duration-fast)]',
              active
                ? 'bg-[var(--color-bg-raised)] text-[var(--color-text)] shadow-[var(--shadow-1)]'
                : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
            )}
          >
            {seg.label}
            {seg.meta !== undefined && (
              <span className="ml-1 font-mono text-[var(--color-text-faint)] tabular-nums">{seg.meta}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
