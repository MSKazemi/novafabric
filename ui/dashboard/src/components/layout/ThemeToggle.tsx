import { useEffect, useState } from 'react';
import { clsx } from 'clsx';

type Theme = 'light' | 'dark' | 'system';
const STORAGE_KEY = 'novafabric.theme';

function readStoredTheme(): Theme {
  if (typeof localStorage === 'undefined') return 'system';
  const v = localStorage.getItem(STORAGE_KEY);
  return v === 'light' || v === 'dark' || v === 'system' ? v : 'system';
}

function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  if (theme === 'system') {
    root.removeAttribute('data-theme');
  } else {
    root.setAttribute('data-theme', theme);
  }
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>('system');

  useEffect(() => {
    const t = readStoredTheme();
    setTheme(t);
    applyTheme(t);
  }, []);

  const choose = (next: Theme) => {
    setTheme(next);
    applyTheme(next);
    if (typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, next);
  };

  const options: { value: Theme; label: string; icon: React.ReactNode }[] = [
    { value: 'light', label: 'Light', icon: <SunIcon /> },
    { value: 'dark', label: 'Dark', icon: <MoonIcon /> },
    { value: 'system', label: 'System', icon: <SystemIcon /> },
  ];

  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className="inline-flex items-center rounded-md border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-0.5"
    >
      {options.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={theme === o.value}
          aria-label={o.label}
          title={`${o.label} theme`}
          onClick={() => choose(o.value)}
          className={clsx(
            'inline-flex items-center justify-center w-7 h-7 rounded transition-colors',
            theme === o.value
              ? 'bg-[var(--color-bg-sunken)] text-[var(--color-text)]'
              : 'text-[var(--color-text-faint)] hover:text-[var(--color-text)]',
          )}
        >
          {o.icon}
        </button>
      ))}
    </div>
  );
}

/** Inline-script that runs BEFORE React hydrates to avoid theme flash.
 *  Astro pages should embed this in <head>. */
export const THEME_BOOTSTRAP_SCRIPT = `
(function(){try{var t=localStorage.getItem('novafabric.theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}catch(e){}})();
`;

function SunIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <circle cx="7" cy="7" r="2.6" stroke="currentColor" strokeWidth="1.4" />
      <path d="M7 1.5V2.5M7 11.5V12.5M1.5 7H2.5M11.5 7H12.5M3.1 3.1l.7.7M10.2 10.2l.7.7M3.1 10.9l.7-.7M10.2 3.8l.7-.7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <path d="M11.7 8.4A4.5 4.5 0 0 1 5.6 2.3a5.5 5.5 0 1 0 6.1 6.1Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  );
}
function SystemIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <rect x="2" y="2.5" width="10" height="7" rx="1" stroke="currentColor" strokeWidth="1.4" />
      <path d="M5 11.5h4M7 9.5v2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
