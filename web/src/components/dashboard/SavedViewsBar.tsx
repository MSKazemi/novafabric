/**
 * SavedViewsBar — E2 (ADR-0201): compact control to save the current list
 * filters as a named view and re-apply or delete saved ones. Presentation
 * only; persistence lives in ../../lib/savedViews (localStorage).
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import { useSavedViews, type SavedView } from '../../lib/savedViews';

export default function SavedViewsBar<T>({
  namespace,
  current,
  onApply,
}: {
  namespace: string;
  current: T;
  onApply: (value: T) => void;
}) {
  const { views, save, remove } = useSavedViews<T>(namespace);
  const [name, setName] = useState('');

  const doSave = () => {
    if (!name.trim()) return;
    save(name, current);
    setName('');
  };

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
        Views:
      </span>
      {views.length === 0 && (
        <span className="text-[10px] text-[var(--color-text-faint)] italic">none saved</span>
      )}
      {views.map((v: SavedView<T>) => (
        <span
          key={v.name}
          className="inline-flex items-center rounded border border-[var(--color-border)] overflow-hidden text-[10px]"
        >
          <button
            onClick={() => onApply(v.value)}
            className="px-2 py-0.5 font-mono text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] transition-colors"
            title={`Apply view "${v.name}"`}
          >
            {v.name}
          </button>
          <button
            onClick={() => remove(v.name)}
            className="px-1.5 py-0.5 text-[var(--color-text-faint)] hover:text-[var(--color-status-failure)] border-l border-[var(--color-border)]"
            title={`Delete view "${v.name}"`}
            aria-label={`Delete view ${v.name}`}
          >
            ×
          </button>
        </span>
      ))}
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && doSave()}
        placeholder="save current as…"
        className="text-[10px] font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-0.5 w-32 focus:border-[var(--color-accent)] focus:outline-none"
      />
      <button
        onClick={doSave}
        disabled={!name.trim()}
        className={clsx(
          'text-[10px] font-mono px-2 py-0.5 rounded border transition-colors',
          name.trim()
            ? 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white'
            : 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed',
        )}
      >
        Save
      </button>
    </div>
  );
}
