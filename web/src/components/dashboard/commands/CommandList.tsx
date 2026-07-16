import { useMemo, useState } from 'react';
import { clsx } from 'clsx';
import { COMMANDS, JOURNEY_LABELS, type CommandDef, type Journey } from './commandRegistry';

const JOURNEY_ORDER: Journey[] = ['debug', 'govern', 'audit', 'infra'];

const JOURNEY_DOT: Record<Journey, string> = {
  debug:  'bg-[var(--color-accent)]',
  govern: 'bg-[var(--color-status-pending)]',
  audit:  'bg-[var(--color-status-success)]',
  infra:  'bg-[var(--color-text-faint)]',
};

function matches(cmd: CommandDef, q: string): boolean {
  if (!q) return true;
  const hay = `${cmd.name} ${cmd.description}`.toLowerCase();
  return q.toLowerCase().split(/\s+/).every((tok) => hay.includes(tok));
}

export default function CommandList({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (cmd: CommandDef) => void;
}) {
  const [query, setQuery] = useState('');

  const byJourney = useMemo(() => {
    const map: Record<Journey, CommandDef[]> = { debug: [], govern: [], audit: [], infra: [] };
    for (const cmd of COMMANDS) {
      if (matches(cmd, query)) map[cmd.journey].push(cmd);
    }
    // Curated defs (no `generated` flag) float to the top of each group; then
    // everything is alphabetical by command name.
    for (const j of JOURNEY_ORDER) {
      map[j].sort((a, b) => {
        const ga = a.generated ? 1 : 0;
        const gb = b.generated ? 1 : 0;
        if (ga !== gb) return ga - gb;
        return a.name.localeCompare(b.name);
      });
    }
    return map;
  }, [query]);

  const total = COMMANDS.filter((c) => matches(c, query)).length;

  return (
    <nav
      aria-label="Command list"
      className="w-64 shrink-0 border-r border-[var(--color-border)] overflow-y-auto flex flex-col"
    >
      <div className="sticky top-0 z-10 bg-[var(--color-bg)] border-b border-[var(--color-border)] p-2">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter commands…"
          aria-label="Filter commands"
          className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] placeholder:text-[var(--color-text-faint)] focus:outline-none focus:border-[var(--color-accent)]"
        />
        <div className="mt-1 px-1 text-[10px] text-[var(--color-text-faint)]">
          {total} of {COMMANDS.length} commands
        </div>
      </div>

      <div className="py-2">
        {JOURNEY_ORDER.map((journey) => {
          const cmds = byJourney[journey];
          if (cmds.length === 0) return null;
          return (
            <div key={journey}>
              <div className="px-4 pt-3 pb-1 text-[10px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] flex items-center justify-between">
                <span>{JOURNEY_LABELS[journey]}</span>
                <span className="text-[var(--color-text-faint)] font-normal">{cmds.length}</span>
              </div>
              {cmds.map((cmd) => {
                const active = cmd.id === selectedId;
                return (
                  <button
                    key={cmd.id}
                    onClick={() => onSelect(cmd)}
                    aria-current={active ? 'true' : undefined}
                    className={clsx(
                      'w-full flex items-center gap-2.5 px-4 py-2 text-left border-l-2 transition-colors',
                      active
                        ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_8%,transparent)]'
                        : 'border-transparent hover:bg-[color-mix(in_oklab,var(--color-bg-raised)_60%,transparent)]',
                    )}
                  >
                    <span
                      aria-hidden="true"
                      className={clsx('w-1.5 h-1.5 rounded-full shrink-0 mt-px', JOURNEY_DOT[journey])}
                    />
                    <div className="min-w-0">
                      <div className={clsx(
                        'text-xs font-mono font-medium truncate',
                        active ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-muted)]',
                      )}>
                        {cmd.name}
                      </div>
                      <div className="text-[10px] text-[var(--color-text-faint)] truncate mt-px">
                        {cmd.description.split('—')[0].trim()}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          );
        })}
        {total === 0 && (
          <div className="px-4 py-6 text-xs text-[var(--color-text-faint)]">
            No commands match “{query}”.
          </div>
        )}
      </div>
    </nav>
  );
}
