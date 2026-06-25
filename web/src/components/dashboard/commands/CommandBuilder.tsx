import { useState, useCallback } from 'react';
import { clsx } from 'clsx';
import { buildCommandString, JOURNEY_LABELS, type CommandDef, type CommandField, type Journey } from './commandRegistry';

const JOURNEY_BADGE: Record<Journey, string> = {
  debug:  'text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)]',
  govern: 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_12%,transparent)]',
  audit:  'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)]',
  infra:  'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)]',
};

function initialValues(cmd: CommandDef): Record<string, string> {
  return Object.fromEntries(
    cmd.fields.map((f) => [f.key, f.defaultValue ?? '']),
  );
}

// CommandsTab renders this with key={selected.id} so React remounts on command
// change — no manual reset logic needed.
export default function CommandBuilder({ cmd }: { cmd: CommandDef }) {
  const [values, setValues] = useState<Record<string, string>>(() => initialValues(cmd));
  const [copied, setCopied] = useState(false);

  const preview = buildCommandString(cmd, values);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(preview);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard denied */
    }
  }, [preview]);

  const setField = useCallback((key: string, val: string) => {
    setValues((prev) => ({ ...prev, [key]: val }));
  }, []);

  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-5">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <h2 className="text-base font-semibold text-[var(--color-text)] font-mono">{cmd.name}</h2>
          <span className={clsx(
            'text-[9px] uppercase tracking-wider px-2 py-px rounded font-medium',
            JOURNEY_BADGE[cmd.journey],
          )}>
            {JOURNEY_LABELS[cmd.journey]}
          </span>
        </div>
        <p className="text-sm text-[var(--color-text-muted)] leading-relaxed">{cmd.description}</p>
      </div>

      {/* Live command preview */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 flex items-start gap-3">
        <span aria-hidden="true" className="text-[var(--color-accent)] font-mono text-sm shrink-0 mt-px">$</span>
        <code className="flex-1 text-sm font-mono text-[var(--color-text)] break-all leading-relaxed">
          {preview || cmd.name}
        </code>
        <button
          onClick={handleCopy}
          aria-label="Copy command to clipboard"
          className={clsx(
            'shrink-0 text-xs px-3 py-1.5 rounded border transition-colors',
            copied
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_40%,transparent)] text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
              : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)]',
          )}
        >
          {copied ? '✓ Copied' : '⎘ Copy'}
        </button>
      </div>

      {/* Fields */}
      <div className="space-y-4">
        {cmd.fields.map((field: CommandField) => {
          // Skip fields whose visibility condition is not met
          if (field.visibleWhen && values[field.visibleWhen.field] !== field.visibleWhen.value) {
            return null;
          }

          return (
            <div key={field.key}>
              {/* Section divider */}
              {field.sectionBefore && (
                <div className="pt-2 pb-1">
                  <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)]">
                    {field.sectionBefore}
                  </span>
                  <hr className="mt-1 border-[var(--color-border)]" />
                </div>
              )}

              <label className="block text-xs font-medium text-[var(--color-text-muted)] mb-1">
                <span className="font-mono">{field.label}</span>
                {field.required && (
                  <span className="ml-1.5 text-[var(--color-status-failure)] text-[10px]">required</span>
                )}
              </label>

              {field.type === 'select' && (
                <select
                  value={values[field.key] ?? field.defaultValue ?? ''}
                  onChange={(e) => setField(field.key, e.target.value)}
                  className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-3 py-1.5 text-sm text-[var(--color-text)] focus:outline-none focus:border-[var(--color-accent)]"
                >
                  {field.options.map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))}
                </select>
              )}

              {field.type === 'toggle' && (
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={values[field.key] === 'true'}
                    onChange={(e) => setField(field.key, e.target.checked ? 'true' : '')}
                    className="accent-[var(--color-accent)] w-3.5 h-3.5"
                  />
                  <span className="text-xs text-[var(--color-text-muted)]">Enable</span>
                </label>
              )}

              {(field.type === 'text' || field.type === 'number') && (() => {
                const listId = field.type === 'text' && 'suggestions' in field && field.suggestions?.length
                  ? `datalist-${field.key}`
                  : undefined;
                return (
                  <>
                    <input
                      type={field.type}
                      list={listId}
                      value={values[field.key] ?? ''}
                      onChange={(e) => setField(field.key, e.target.value)}
                      placeholder={field.required ? '(required)' : '(optional)'}
                      className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-3 py-1.5 text-sm font-mono text-[var(--color-text)] placeholder:text-[var(--color-text-faint)] focus:outline-none focus:border-[var(--color-accent)]"
                    />
                    {listId && (
                      <datalist id={listId}>
                        {'suggestions' in field && field.suggestions?.map((s) => (
                          <option key={s} value={s} />
                        ))}
                      </datalist>
                    )}
                  </>
                );
              })()}

              <p className="mt-1 text-[11px] text-[var(--color-text-faint)]">{field.hint}</p>
            </div>
          );
        })}
      </div>

      {/* Native tab note */}
      {cmd.nativeTabNote && (
        <p className="text-[11px] text-[var(--color-text-faint)] italic">{cmd.nativeTabNote}</p>
      )}

      {/* Layer C warning */}
      <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] px-4 py-3 text-xs text-[var(--color-text-muted)] leading-relaxed">
        <strong className="text-[var(--color-text)]">Copy only</strong> — the dashboard does not execute commands directly (Layer C, per ADR-0027). Run the copied command in your terminal. The Runs tab auto-refreshes when the result appears.
      </div>

      {/* Docs link */}
      <a
        href={cmd.docsPath}
        className="inline-flex items-center gap-1 text-[11px] text-[var(--color-text-faint)] hover:text-[var(--color-text-muted)] transition-colors"
      >
        ↗ Full reference for {cmd.name}
      </a>
    </div>
  );
}
