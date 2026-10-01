/**
 * Runs filter bar — ADR-0232 D1 (grammar), D3 (observed-value suggestions),
 * ADR-0233 (scope), ADR-0234 D2 (honest degradation).
 *
 * The bar is a typing convenience over `nova query --where`, never a second
 * query language: the server parses with the DSL's own parser and every result
 * shows the equivalent CLI. Draft text is transient (ADR-0232 D2 carve-out) —
 * only the *applied* filter and scope reach the URL.
 */
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { clsx } from 'clsx';
import { api, FILTER_DIMENSIONS, type FilterRunsResult, type FilterSuggestResult } from '../../../../lib/api';
import CopyButton from '../../../ui/CopyButton';
import { activeTerm, completeTerm, FILTER_SCOPES, SCOPE_LABEL, type FilterScope } from './viewState';

export interface FilterBarProps {
  /** The applied filter (from the URL). */
  value: string;
  onApply: (text: string) => void;
  scope: FilterScope;
  onScopeChange: (scope: FilterScope) => void;
  result: FilterRunsResult | null;
  loading: boolean;
  error: string | null;
  /** Injectable for tests; defaults to the live endpoint. */
  suggest?: (dimension: string) => Promise<FilterSuggestResult>;
}

const MAX_VISIBLE_SUGGESTIONS = 8;

export default function FilterBar({
  value,
  onApply,
  scope,
  onScopeChange,
  result,
  loading,
  error,
  suggest = api.suggestFilterValues,
}: FilterBarProps) {
  const [draft, setDraft] = useState(value);
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const [suggestions, setSuggestions] = useState<FilterSuggestResult | null>(null);
  const cache = useRef(new Map<string, FilterSuggestResult>());
  const listId = useId();
  const errorId = useId();

  // Back/forward or a pasted link changes the applied filter: follow it.
  useEffect(() => { setDraft(value); }, [value]);

  const term = activeTerm(draft);
  const dimension = term && (FILTER_DIMENSIONS as readonly string[]).includes(term.dimension)
    ? term.dimension
    : null;

  useEffect(() => {
    if (!dimension) { setSuggestions(null); return; }
    const hit = cache.current.get(dimension);
    if (hit) { setSuggestions(hit); return; }
    let cancelled = false;
    suggest(dimension)
      .then(r => { cache.current.set(dimension, r); if (!cancelled) setSuggestions(r); })
      .catch(() => { if (!cancelled) setSuggestions(null); });
    return () => { cancelled = true; };
  }, [dimension, suggest]);

  const options = useMemo(() => {
    if (!suggestions || !term || suggestions.dimension !== term.dimension) return [];
    const p = term.partial.toLowerCase();
    return suggestions.values.filter(v => v.toLowerCase().startsWith(p)).slice(0, MAX_VISIBLE_SUGGESTIONS);
  }, [suggestions, term]);

  const showList = open && options.length > 0;

  function apply(text: string) {
    setOpen(false);
    setHighlight(-1);
    onApply(text.trim());
  }

  function choose(v: string) {
    setDraft(completeTerm(draft, v));
    setHighlight(-1);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown' && options.length > 0) {
      e.preventDefault();
      setOpen(true);
      setHighlight(h => (h + 1) % options.length);
    } else if (e.key === 'ArrowUp' && options.length > 0) {
      e.preventDefault();
      setHighlight(h => (h <= 0 ? options.length - 1 : h - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (showList && highlight >= 0) choose(options[highlight]!);
      else apply(draft);
    } else if (e.key === 'Escape') {
      if (showList) { e.preventDefault(); e.stopPropagation(); setOpen(false); }
    }
  }

  return (
    <div className="space-y-1.5" data-testid="filter-bar">
      <div className="relative">
        <label htmlFor={`${listId}-input`} className="sr-only">Filter runs</label>
        <input
          id={`${listId}-input`}
          type="text"
          role="combobox"
          aria-expanded={showList}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={showList && highlight >= 0 ? `${listId}-opt-${highlight}` : undefined}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          value={draft}
          onChange={e => { setDraft(e.target.value); setOpen(true); setHighlight(-1); }}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
          spellCheck={false}
          autoComplete="off"
          placeholder="Filter: status:error -model:gpt-4o  (Enter to apply)"
          className={clsx(
            'w-full text-xs rounded border bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:outline-none',
            error
              ? 'border-[var(--color-status-failure)]'
              : 'border-[var(--color-border)] focus:border-[var(--color-accent)]',
          )}
        />
        {showList && (
          <ul
            id={listId}
            role="listbox"
            aria-label={`Observed ${dimension} values`}
            className="absolute z-20 mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-lg text-xs font-mono max-h-56 overflow-y-auto"
          >
            {options.map((opt, i) => (
              <li
                key={opt}
                id={`${listId}-opt-${i}`}
                role="option"
                aria-selected={i === highlight}
                onMouseDown={e => { e.preventDefault(); choose(opt); }}
                className={clsx(
                  'px-2 py-1 cursor-pointer truncate',
                  i === highlight ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]' : 'hover:bg-[var(--color-bg-sunken)]',
                )}
              >
                {opt}
              </li>
            ))}
            {suggestions?.truncated && (
              <li role="presentation" className="px-2 py-1 text-2xs text-[var(--color-text-faint)] border-t border-[var(--color-border)]">
                Partial list — more {dimension} values exist; type to narrow.
              </li>
            )}
          </ul>
        )}
      </div>

      <div className="flex items-center gap-1.5 flex-wrap text-[10px]">
        <span className="text-[var(--color-text-faint)]" id={`${listId}-scope`}>Scope</span>
        <div role="radiogroup" aria-labelledby={`${listId}-scope`} className="inline-flex rounded border border-[var(--color-border)] overflow-hidden">
          {FILTER_SCOPES.map(s => (
            <button
              key={s}
              type="button"
              role="radio"
              aria-checked={scope === s}
              title={SCOPE_LABEL[s]}
              onClick={() => onScopeChange(s)}
              className={clsx(
                'px-2 py-0.5 font-mono transition-colors',
                scope === s
                  ? 'bg-[var(--color-accent)] text-[var(--color-accent-fg)]'
                  : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
              )}
            >
              {s}
            </button>
          ))}
        </div>
        {value && (
          <button
            type="button"
            onClick={() => { setDraft(''); apply(''); }}
            className="px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
          >
            Clear filter
          </button>
        )}
        {loading && <span role="status" className="text-[var(--color-text-faint)]">filtering…</span>}
      </div>

      {error && (
        <p id={errorId} role="alert" className="text-[10px] text-[var(--color-status-failure)] break-words">
          {error}
        </p>
      )}

      {value && result && !error && (
        <div className="space-y-1">
          <p className="text-[10px] text-[var(--color-text-muted)]" data-testid="filter-summary">
            {result.items.length} of {result.matched} {SCOPE_LABEL[result.scope]}
            {result.truncated && ' — list truncated, narrow the filter to see the rest'}
          </p>
          {!result.complete && result.incomplete_reasons.length > 0 && (
            <div role="note" className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[10px] text-[var(--color-text-muted)]">
              <p className="font-medium">This result may be incomplete:</p>
              <ul className="list-disc pl-4">
                {result.incomplete_reasons.slice(0, 5).map(r => <li key={r}>{r}</li>)}
                {result.incomplete_reasons.length > 5 && (
                  <li>…and {result.incomplete_reasons.length - 5} more</li>
                )}
              </ul>
            </div>
          )}
          <div className="flex items-center gap-1.5 min-w-0">
            <code className="flex-1 truncate text-[10px] font-mono text-[var(--color-text-faint)]" title={result.cli_equivalent}>
              {result.cli_equivalent}
            </code>
            <CopyButton text={result.cli_equivalent} label="Copy CLI" className="shrink-0" />
          </div>
        </div>
      )}
    </div>
  );
}
