import { useEffect, useMemo, useRef, useState } from 'react';

export interface Command {
  id: string;
  label: string;
  /** Optional group/section shown as a dim suffix. */
  group?: string;
  /** Optional keyword string to widen fuzzy matching. */
  keywords?: string;
  run: () => void;
}

interface Props {
  commands: Command[];
  onClose: () => void;
  /**
   * Optional async provider for live entity results (runs, assets, incidents…).
   * Called debounced as the user types; results are appended below static matches.
   */
  search?: (query: string) => Promise<Command[]>;
}

/** Subsequence fuzzy match: returns a score (lower = better) or null if no match. */
function fuzzyScore(query: string, text: string): number | null {
  if (!query) return 0;
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  let ti = 0;
  let score = 0;
  let lastHit = -1;
  for (let qi = 0; qi < q.length; qi++) {
    const c = q[qi];
    const found = t.indexOf(c, ti);
    if (found === -1) return null;
    if (lastHit !== -1) score += found - lastHit; // reward adjacency
    lastHit = found;
    ti = found + 1;
  }
  return score + found_offset(t, q);
}

function found_offset(t: string, q: string): number {
  // Prefer earlier first-match and exact substring hits.
  const idx = t.indexOf(q);
  return idx === 0 ? -5 : idx > 0 ? -2 : 0;
}

/**
 * Global command palette (Cmd/Ctrl+K). Fuzzy-search across navigation targets
 * and actions, arrow keys to move, Enter to run, Esc to close.
 */
export default function CommandPalette({ commands, onClose, search }: Props) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const [dynamic, setDynamic] = useState<Command[]>([]);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Live entity search: debounced, min 2 chars. Stale responses are dropped.
  useEffect(() => {
    if (!search || query.trim().length < 2) { setDynamic([]); setSearching(false); return; }
    let cancelled = false;
    setSearching(true);
    const handle = setTimeout(() => {
      search(query.trim())
        .then((cmds) => { if (!cancelled) setDynamic(cmds); })
        .catch(() => { if (!cancelled) setDynamic([]); })
        .finally(() => { if (!cancelled) setSearching(false); });
    }, 220);
    return () => { cancelled = true; clearTimeout(handle); };
  }, [query, search]);

  const results = useMemo(() => {
    const scored = commands
      .map(c => ({ c, s: fuzzyScore(query, `${c.label} ${c.keywords ?? ''} ${c.group ?? ''}`) }))
      .filter((r): r is { c: Command; s: number } => r.s !== null)
      .sort((a, b) => a.s - b.s)
      .map(r => r.c);
    return [...scored, ...dynamic];
  }, [commands, query, dynamic]);

  useEffect(() => { setActive(0); }, [query]);
  useEffect(() => { inputRef.current?.focus(); }, []);

  // Keep the active row in view.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(a => Math.min(a + 1, results.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(a => Math.max(a - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); const c = results[active]; if (c) { c.run(); onClose(); } }
    else if (e.key === 'Escape') { e.preventDefault(); onClose(); }
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-start justify-center pt-[12vh] bg-black/40"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div
        className="w-full max-w-xl mx-4 rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] shadow-2xl overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Jump to a view or action…"
          aria-label="Search commands"
          className="w-full px-4 py-3 bg-transparent text-sm text-[var(--color-text)] outline-none border-b border-[var(--color-border)] placeholder:text-[var(--color-text-faint)]"
        />
        <div ref={listRef} className="max-h-80 overflow-y-auto py-1">
          {results.length === 0 ? (
            <p className="px-4 py-6 text-center text-xs text-[var(--color-text-faint)]">{searching ? 'Searching…' : 'No matches.'}</p>
          ) : (
            results.map((c, i) => (
              <button
                key={c.id}
                data-idx={i}
                onMouseEnter={() => setActive(i)}
                onClick={() => { c.run(); onClose(); }}
                className={[
                  'w-full flex items-center justify-between gap-3 px-4 py-2 text-left text-sm transition-colors',
                  i === active
                    ? 'bg-[color-mix(in_oklab,var(--color-accent)_14%,transparent)] text-[var(--color-text)]'
                    : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                ].join(' ')}
              >
                <span className="truncate">{c.label}</span>
                {c.group && <span className="shrink-0 text-[10px] font-mono text-[var(--color-text-faint)]">{c.group}</span>}
              </button>
            ))
          )}
        </div>
        <div className="px-4 py-2 border-t border-[var(--color-border)] text-[10px] font-mono text-[var(--color-text-faint)] flex gap-3">
          <span>↑↓ navigate</span><span>↵ select</span><span>esc close</span>
        </div>
      </div>
    </div>
  );
}
