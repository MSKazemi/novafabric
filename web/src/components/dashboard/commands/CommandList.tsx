import { clsx } from 'clsx';
import { COMMANDS, JOURNEY_LABELS, type CommandDef, type Journey } from './commandRegistry';

const JOURNEY_ORDER: Journey[] = ['debug', 'govern', 'audit', 'infra'];

const JOURNEY_DOT: Record<Journey, string> = {
  debug:  'bg-[var(--color-accent)]',
  govern: 'bg-[var(--color-status-pending)]',
  audit:  'bg-[var(--color-status-success)]',
  infra:  'bg-[var(--color-text-faint)]',
};

export default function CommandList({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (cmd: CommandDef) => void;
}) {
  return (
    <nav
      aria-label="Command list"
      className="w-56 shrink-0 border-r border-[var(--color-border)] overflow-y-auto py-2"
    >
      {JOURNEY_ORDER.map((journey) => {
        const cmds = COMMANDS.filter((c) => c.journey === journey);
        return (
          <div key={journey}>
            <div className="px-4 pt-3 pb-1 text-[10px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)]">
              {JOURNEY_LABELS[journey]}
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
    </nav>
  );
}
