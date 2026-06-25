import { useEffect, useRef, useState } from 'react';
import { clsx } from 'clsx';

type Theme = 'light' | 'dark' | 'system';
type Accent = 'green' | 'violet' | 'cyan' | 'amber' | 'rose';
type Density = 'comfortable' | 'compact';

const THEME_KEY = 'novafabric.theme';
const ACCENT_KEY = 'novafabric.accent';
const DENSITY_KEY = 'novafabric.density';

function readLocal<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return (allowed as readonly string[]).includes(v ?? '') ? (v as T) : fallback;
  } catch { return fallback; }
}

export function applyAppearance(theme: Theme, accent: Accent, density: Density): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  root.setAttribute('data-accent', accent);
  if (density === 'compact') root.setAttribute('data-density', 'compact');
  else root.removeAttribute('data-density');
}

const ACCENTS: ReadonlyArray<{ id: Accent; darkColor: string; lightColor: string; label: string }> = [
  { id: 'green',  darkColor: '#c4f0a8', lightColor: '#2f6a14', label: 'Green'  },
  { id: 'violet', darkColor: '#c4a8f0', lightColor: '#6b2fb3', label: 'Violet' },
  { id: 'cyan',   darkColor: '#a8e4f0', lightColor: '#0e7a96', label: 'Cyan'   },
  { id: 'amber',  darkColor: '#f0d4a8', lightColor: '#8a5a00', label: 'Amber'  },
  { id: 'rose',   darkColor: '#f0a8c4', lightColor: '#b02060', label: 'Rose'   },
];

const THEME_OPTS: Array<{ v: Theme; label: string; icon: string }> = [
  { v: 'light',  label: 'Light',  icon: '☀' },
  { v: 'dark',   label: 'Dark',   icon: '☾' },
  { v: 'system', label: 'System', icon: '⊙' },
];

const DENSITY_OPTS: Array<{ v: Density; label: string }> = [
  { v: 'comfortable', label: 'Comfortable' },
  { v: 'compact',     label: 'Compact' },
];

interface Props {
  sidebarCollapsed: boolean;
  triggerRef: React.RefObject<HTMLButtonElement | null>;
  onClose: () => void;
}

export default function AppearancePanel({ sidebarCollapsed, triggerRef, onClose }: Props) {
  const [theme, setTheme] = useState<Theme>(() =>
    readLocal(THEME_KEY, ['light', 'dark', 'system'] as const, 'system')
  );
  const [accent, setAccent] = useState<Accent>(() =>
    readLocal(ACCENT_KEY, ['green', 'violet', 'cyan', 'amber', 'rose'] as const, 'green')
  );
  const [density, setDensity] = useState<Density>(() =>
    readLocal(DENSITY_KEY, ['comfortable', 'compact'] as const, 'comfortable')
  );
  const [isDark, setIsDark] = useState(true);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const check = () => {
      const attr = document.documentElement.getAttribute('data-theme');
      if (attr === 'light') setIsDark(false);
      else if (attr === 'dark') setIsDark(true);
      else setIsDark(window.matchMedia('(prefers-color-scheme: dark)').matches);
    };
    check();
    const obs = new MutationObserver(check);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || triggerRef.current?.contains(t)) return;
      onClose();
    };
    // Capture phase so we win over other keydown listeners and can stop propagation
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
    };
    document.addEventListener('mousedown', onMouseDown);
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('mousedown', onMouseDown);
      document.removeEventListener('keydown', onKeyDown, true);
    };
  }, [onClose, triggerRef]);

  const apply = (t: Theme, a: Accent, d: Density) => {
    setTheme(t); setAccent(a); setDensity(d);
    applyAppearance(t, a, d);
    try {
      localStorage.setItem(THEME_KEY, t);
      localStorage.setItem(ACCENT_KEY, a);
      localStorage.setItem(DENSITY_KEY, d);
    } catch { /* storage unavailable */ }
  };

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-label="Appearance settings"
      style={{ left: `calc(var(${sidebarCollapsed ? '--sidebar-w-collapsed' : '--sidebar-w'}) + 4px)` }}
      className="fixed z-50 bottom-10 w-52 rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] shadow-2xl p-3 space-y-3"
    >
      {/* Theme */}
      <section>
        <p className="text-[9px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] mb-1.5 select-none">
          Theme
        </p>
        <div className="flex gap-1">
          {THEME_OPTS.map(({ v, label, icon }) => (
            <button
              key={v}
              onClick={() => apply(v, accent, density)}
              className={clsx(
                'flex-1 flex flex-col items-center gap-0.5 py-1.5 rounded border text-[10px] transition-colors',
                theme === v
                  ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)] text-[var(--color-text)]'
                  : 'border-[var(--color-border)] text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)]',
              )}
            >
              <span className="text-sm leading-none">{icon}</span>
              <span>{label}</span>
            </button>
          ))}
        </div>
      </section>

      {/* Accent */}
      <section>
        <p className="text-[9px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] mb-1.5 select-none">
          Accent
        </p>
        <div className="flex items-center gap-2">
          {ACCENTS.map(({ id, darkColor, lightColor, label }) => (
            <button
              key={id}
              title={label}
              onClick={() => apply(theme, id, density)}
              className={clsx(
                'w-6 h-6 rounded-full border-2 transition-transform hover:scale-110',
                accent === id ? 'border-[var(--color-text)] scale-110' : 'border-transparent',
              )}
              style={{ backgroundColor: isDark ? darkColor : lightColor }}
            />
          ))}
        </div>
      </section>

      {/* Density */}
      <section>
        <p className="text-[9px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] mb-1.5 select-none">
          Density
        </p>
        <div className="flex gap-1">
          {DENSITY_OPTS.map(({ v, label }) => (
            <button
              key={v}
              onClick={() => apply(theme, accent, v)}
              className={clsx(
                'flex-1 py-1 rounded border text-[10px] transition-colors',
                density === v
                  ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)] text-[var(--color-text)]'
                  : 'border-[var(--color-border)] text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)]',
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
