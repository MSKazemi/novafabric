import { useRef, useState } from 'react';
import { clsx } from 'clsx';
import AppearancePanel from './AppearancePanel';

export type Tab = 'home' | 'runs' | 'registry' | 'lineage' | 'diff' | 'capture' | 'audit' | 'evidence' | 'holds' | 'policy' | 'seal' | 'infra' | 'commands' | 'admin' | 'compliance' | 'governance' | 'kg' | 'cost' | 'schema' | 'reports' | 'eval' | 'risk' | 'storage' | 'incidents' | 'spine' | 'ops';

interface NavItem {
  id: Tab;
  label: string;
  icon: string;
  badge?: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Overview',
    items: [
      { id: 'home', label: 'Home', icon: '⌂' },
    ],
  },
  {
    label: 'Debug & Investigate',
    items: [
      { id: 'runs',  label: 'Runs', icon: '▷' },
      { id: 'diff',  label: 'Diff', icon: '⊘' },
    ],
  },
  {
    label: 'Govern & Promote',
    items: [
      { id: 'registry',   label: 'Registry',   icon: '◈' },
      { id: 'governance', label: 'Governance',  icon: '⚖', badge: 'Gov' },
      { id: 'eval',       label: 'Eval',        icon: '◇', badge: 'eval' },
      { id: 'risk',       label: 'Risk',        icon: '⚠', badge: 'risk' },
    ],
  },
  {
    label: 'Audit & Verify',
    items: [
      { id: 'lineage',  label: 'Lineage',  icon: '⬡' },
      { id: 'kg',       label: 'KG',       icon: '✦', badge: 'KG' },
      { id: 'cost',     label: 'Cost',     icon: '$',  badge: 'cost' },
      { id: 'schema',   label: 'Schema',   icon: '⊕',  badge: 'spec' },
      { id: 'evidence', label: 'Evidence', icon: '⊛' },
      { id: 'audit',    label: 'Audit',    icon: '◎' },
      { id: 'holds',    label: 'Holds',    icon: '⊗' },
      { id: 'policy',   label: 'Policy',   icon: '⊙' },
      { id: 'seal',       label: 'Seal',       icon: '⊚', badge: 'SoD' },
      { id: 'spine',      label: 'Spine',      icon: '⚓', badge: 'D3' },
      { id: 'compliance', label: 'Compliance', icon: '⚖', badge: 'Reg' },
      { id: 'incidents',  label: 'Incidents',  icon: '⚑', badge: 'Art73' },
      { id: 'capture',    label: 'Capture',    icon: '⊕', badge: 'L·C' },
    ],
  },
  {
    label: 'Infrastructure',
    items: [
      { id: 'infra',    label: 'Infra',    icon: '⬢' },
      { id: 'storage',  label: 'Storage',  icon: '⛁', badge: 'WORM' },
      { id: 'ops',      label: 'Ops',      icon: '⚙' },
    ],
  },
  {
    label: 'Admin',
    items: [
      { id: 'admin',    label: 'Admin',    icon: '⊞' },
    ],
  },
  {
    label: 'CLI Commands',
    items: [
      { id: 'commands', label: 'Commands', icon: '$', badge: 'CLI' },
    ],
  },
  {
    label: 'Reports',
    items: [
      { id: 'reports', label: 'Reports', icon: '⊟' },
    ],
  },
];

export const ALL_TABS: Tab[] = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.id));

function GearIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true" className="shrink-0">
      <circle cx="6" cy="6" r="1.5" stroke="currentColor" strokeWidth="1.3" />
      <path d="M6 1v1M6 10v1M1 6h1M10 6h1M2.6 2.6l.7.7M8.7 8.7l.7.7M2.6 9.4l.7-.7M8.7 3.3l.7-.7" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

interface SidebarProps {
  tab: Tab;
  onTabChange: (t: Tab) => void;
  counts: Partial<Record<Tab, number>>;
  serverInfo: { version: string; base: string } | null;
  onDisconnect: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  autoRefresh: boolean;
  onAutoRefreshChange: (v: boolean) => void;
  onHelpOpen: () => void;
}

export default function Sidebar({
  tab, onTabChange, counts, serverInfo, onDisconnect,
  collapsed, onToggleCollapse, autoRefresh, onAutoRefreshChange, onHelpOpen,
}: SidebarProps) {
  const [showAppearance, setShowAppearance] = useState(false);
  const gearRef = useRef<HTMLButtonElement>(null);

  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem('novafabric.sidebar-groups');
      return raw ? new Set(JSON.parse(raw) as string[]) : new Set<string>();
    } catch {
      return new Set<string>();
    }
  });

  const toggleGroup = (label: string, activeTabInGroup: boolean) => {
    if (activeTabInGroup) return;
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (next.has(label)) { next.delete(label); } else { next.add(label); }
      try { localStorage.setItem('novafabric.sidebar-groups', JSON.stringify([...next])); } catch { /* ignore */ }
      return next;
    });
  };

  return (
    <aside
      aria-label="Dashboard navigation"
      className={clsx(
        'flex flex-col shrink-0 border-r border-[var(--color-border)] bg-[var(--color-bg-sunken)] transition-[width] duration-150 overflow-hidden',
        collapsed ? 'w-11' : 'w-52',
      )}
    >
      {/* Logo + collapse toggle */}
      <div className={clsx(
        'flex items-center border-b border-[var(--color-border)] h-11 shrink-0 px-2 gap-2',
        collapsed ? 'justify-center' : 'justify-between',
      )}>
        {collapsed ? (
          <svg viewBox="0 0 32 32" width="22" height="22" fill="none" aria-label="NovaFabric" xmlns="http://www.w3.org/2000/svg" className="shrink-0">
            <rect width="32" height="32" rx="7" fill="#111114"/>
            <polygon points="16,3.5 27.5,10 27.5,22 16,28.5 4.5,22 4.5,10" stroke="#c4f0a8" strokeWidth="1.5" fill="none" strokeLinejoin="round"/>
            <path d="M10 23 L10 9 L22 23 L22 9" stroke="#c4f0a8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
          </svg>
        ) : (
          <span className="text-[10px] font-medium uppercase tracking-widest text-[var(--color-text-faint)] font-mono select-none">
            NovaFabric
          </span>
        )}
        <button
          onClick={onToggleCollapse}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className="w-6 h-6 flex items-center justify-center rounded text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] transition-colors shrink-0"
        >
          <span aria-hidden="true" className="text-xs leading-none">{collapsed ? '›' : '‹'}</span>
        </button>
      </div>

      {/* Nav items */}
      <nav className="flex-1 overflow-y-auto py-1" role="navigation">
        {NAV_GROUPS.map((group) => {
          const groupHasActive = group.items.some(i => i.id === tab);
          const groupCollapsed = !collapsed && collapsedGroups.has(group.label) && !groupHasActive;
          return (
            <div key={group.label}>
              {!collapsed && (
                <button
                  type="button"
                  onClick={() => toggleGroup(group.label, groupHasActive)}
                  className="w-full flex items-center justify-between px-3 pt-3 pb-0.5 text-[9px] font-semibold uppercase tracking-widest text-[var(--color-text-faint)] select-none hover:text-[var(--color-text-muted)] transition-colors"
                >
                  <span>{group.label}</span>
                  <span aria-hidden="true" className="text-[8px]">
                    {groupCollapsed ? `▸ ${group.items.length}` : '▾'}
                  </span>
                </button>
              )}
              {!groupCollapsed && group.items.map((item) => {
                const active = tab === item.id;
                const count = counts[item.id];
                const shortcutIdx = ALL_TABS.indexOf(item.id) + 1;
                return (
                  <button
                    key={item.id}
                    onClick={() => onTabChange(item.id)}
                    title={collapsed ? `${item.label} (${shortcutIdx})` : `${item.label} — press ${shortcutIdx}`}
                    aria-current={active ? 'page' : undefined}
                    className={clsx(
                      'relative w-full flex items-center gap-2.5 px-3 py-2 text-xs transition-colors group',
                      active
                        ? 'text-[var(--color-text)] bg-[var(--color-bg-raised)]'
                        : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[color-mix(in_oklab,var(--color-bg-raised)_60%,transparent)]',
                    )}
                  >
                    {active && (
                      <span
                        aria-hidden="true"
                        className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r bg-[var(--color-accent)]"
                      />
                    )}
                    <span aria-hidden="true" className="w-4 text-center shrink-0 text-[var(--color-text-faint)]">{item.icon}</span>
                    {!collapsed && (
                      <>
                        <span className="flex-1 text-left font-medium">{item.label}</span>
                        {item.badge && (
                          <span className="text-[9px] uppercase tracking-wider px-1 py-px rounded bg-[color-mix(in_oklab,var(--color-text-faint)_12%,transparent)] text-[var(--color-text-faint)] shrink-0">
                            {item.badge}
                          </span>
                        )}
                        {count !== undefined && !item.badge && (
                          <span className={clsx(
                            'text-[10px] font-mono shrink-0 tabular-nums',
                            active ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-faint)]',
                          )}>
                            {count}
                          </span>
                        )}
                        <span
                          aria-hidden="true"
                          className="absolute right-2 text-[9px] text-[var(--color-text-faint)] opacity-0 group-hover:opacity-60 font-mono transition-opacity"
                        >
                          {shortcutIdx}
                        </span>
                      </>
                    )}
                  </button>
                );
              })}
            </div>
          );
        })}
      </nav>

      {/* Footer: auto-refresh + server info + help + disconnect */}
      <div className="border-t border-[var(--color-border)] py-2 px-2 space-y-1 shrink-0">
        {/* Auto-refresh toggle */}
        {!collapsed && (
          <label className="flex items-center gap-1.5 px-1 py-1 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => onAutoRefreshChange(e.target.checked)}
              className="accent-[var(--color-accent)] w-3 h-3"
            />
            <span className={clsx(
              'text-[10px] font-mono transition-colors',
              autoRefresh ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-faint)]',
            )}>
              auto {autoRefresh && '·5s'}
            </span>
          </label>
        )}

        {/* Connection dot when collapsed */}
        {serverInfo && collapsed && (
          <div className="flex justify-center py-1">
            <span
              className="w-2 h-2 rounded-full bg-[var(--color-status-success)] shrink-0"
              aria-label="Connected"
              title={`Connected · ${serverInfo.base}`}
            />
          </div>
        )}

        {/* Server info */}
        {serverInfo && !collapsed && (
          <div className="px-1 py-1 space-y-0.5">
            <div className="flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-status-success)] shrink-0" aria-hidden="true" />
              <code className="text-[9px] font-mono text-[var(--color-text-faint)] truncate">{serverInfo.base}</code>
            </div>
            <div className="pl-3 flex items-center gap-2">
              <span className="text-[9px] font-mono text-[var(--color-text-faint)]">v{serverInfo.version}</span>
              <span className="text-[8px] uppercase tracking-wider px-1 py-px rounded bg-[color-mix(in_oklab,var(--color-edge-derived-from)_15%,transparent)] text-[var(--color-edge-derived-from)]">
                experimental
              </span>
            </div>
          </div>
        )}

        {/* Help */}
        <button
          onClick={onHelpOpen}
          title="Keyboard shortcuts (?)"
          className={clsx(
            'w-full flex items-center gap-2 px-1 py-1 rounded text-[10px] text-[var(--color-text-faint)] hover:text-[var(--color-text-muted)] transition-colors',
            collapsed ? 'justify-center' : '',
          )}
        >
          <span aria-hidden="true" className="shrink-0 text-xs">?</span>
          {!collapsed && <span>Keyboard shortcuts</span>}
        </button>

        {/* Appearance */}
        <button
          ref={gearRef}
          onClick={() => setShowAppearance(v => !v)}
          title="Appearance"
          className={clsx(
            'w-full flex items-center gap-2 px-1 py-1 rounded text-[10px] transition-colors',
            showAppearance
              ? 'text-[var(--color-text)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]'
              : 'text-[var(--color-text-faint)] hover:text-[var(--color-text-muted)]',
            collapsed ? 'justify-center' : '',
          )}
        >
          <GearIcon />
          {!collapsed && <span>Appearance</span>}
        </button>

        {showAppearance && (
          <AppearancePanel
            sidebarCollapsed={collapsed}
            triggerRef={gearRef}
            onClose={() => setShowAppearance(false)}
          />
        )}

        {/* Disconnect */}
        <button
          onClick={onDisconnect}
          title="Disconnect"
          className={clsx(
            'w-full flex items-center gap-2 px-1 py-1 rounded text-[10px] text-[var(--color-text-faint)] hover:text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] transition-colors',
            collapsed ? 'justify-center' : '',
          )}
        >
          <span aria-hidden="true" className="shrink-0">⏏</span>
          {!collapsed && <span>Disconnect</span>}
        </button>
      </div>
    </aside>
  );
}
