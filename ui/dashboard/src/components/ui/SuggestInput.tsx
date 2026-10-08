import { useState, useRef, useEffect } from 'react';

interface SuggestInputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'onKeyDown'> {
  value: string;
  onChange: (v: string) => void;
  suggestions: string[];
  /** Called when the user presses Enter (after closing the dropdown). */
  onEnter?: () => void;
}

/**
 * Text input with a live-filtered suggestion dropdown.
 * Shows up to 8 suggestions (top of list when empty, filtered when typing).
 * Click-outside closes the dropdown; Escape closes without selecting.
 * Enter fires onEnter and bubbles naturally so forms still submit.
 */
export function SuggestInput({ value, onChange, suggestions, onEnter, className, ...rest }: SuggestInputProps) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const q = value.trim().toLowerCase();
  const filtered = q
    ? suggestions.filter(s => s.toLowerCase().includes(q)).slice(0, 8)
    : suggestions.slice(0, 8);

  return (
    <div ref={wrapRef} className="relative">
      <input
        {...rest}
        type="text"
        value={value}
        onChange={e => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onKeyDown={e => {
          if (e.key === 'Escape') { setOpen(false); e.preventDefault(); return; }
          if (e.key === 'Enter') { setOpen(false); onEnter?.(); }
        }}
        className={className}
      />
      {open && filtered.length > 0 && (
        <ul className="absolute z-50 left-0 right-0 top-full mt-0.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-lg max-h-48 overflow-y-auto">
          {filtered.map(s => (
            <li key={s}>
              <button
                type="button"
                onMouseDown={e => { e.preventDefault(); onChange(s); setOpen(false); }}
                className="w-full text-left px-2.5 py-1.5 font-mono text-xs text-[var(--color-text)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] truncate"
              >
                {s}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
