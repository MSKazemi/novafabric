/**
 * The ADR-0199 honest-truncation affordance: any bounded list must tell the
 * user how much more exists server-side and offer a way to get at it —
 * silent truncation is a defect class.
 */
import Button from './primitives/Button';

export interface TruncationNoticeProps {
  /** Items currently shown. */
  shown: number;
  /** Server-side total, if reported. */
  total?: number | null;
  /** Whether the total is approximate (shows a ~ prefix). */
  approximate?: boolean;
  /** True when more pages exist. */
  hasMore: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  /** Optional hint when there is no load-more path (e.g. "refine filters"). */
  hint?: string;
}

export default function TruncationNotice({
  shown,
  total,
  approximate = false,
  hasMore,
  loadingMore = false,
  onLoadMore,
  hint,
}: TruncationNoticeProps) {
  if (!hasMore && (total == null || shown >= total)) return null;

  const remaining = total != null && total > shown ? (total - shown).toLocaleString() : null;

  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2 text-2xs text-[var(--color-text-faint)] border-t border-[var(--color-border)] bg-[var(--color-bg-sunken)]">
      <span className="font-mono tabular-nums">
        {total != null ? (
          <>
            Showing {shown.toLocaleString()} of {approximate ? '~' : ''}{total.toLocaleString()}
            {remaining && <span> — {remaining} more</span>}
          </>
        ) : (
          <>Showing {shown.toLocaleString()} — more available</>
        )}
      </span>
      <span className="flex items-center gap-2">
        {hint && <span>{hint}</span>}
        {hasMore && onLoadMore && (
          <Button size="sm" variant="ghost" pending={loadingMore} onClick={onLoadMore}>
            Load more
          </Button>
        )}
      </span>
    </div>
  );
}
