import { useEffect, useRef, useState } from 'react';
import { clsx } from 'clsx';
import AppearancePanel from './AppearancePanel';
import Badge from '../ui/primitives/Badge';
import Icon from '../ui/primitives/Icon';

export type Tab = 'home' | 'analytics' | 'runs' | 'registry' | 'lineage' | 'diff' | 'capture' | 'audit' | 'evidence' | 'holds' | 'policy' | 'seal' | 'infra' | 'commands' | 'admin' | 'compliance' | 'governance' | 'kg' | 'cost' | 'schema' | 'reports' | 'eval' | 'risk' | 'storage' | 'incidents' | 'spine' | 'ops' | 'alerts' | 'export';

interface NavItem {
  id: Tab;
  label: string;
  badge?: string;
  /** Second key of the stable `g`-prefixed navigation shortcut (e.g. 'h' → g h). */
  shortcut: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

/**
 * Information architecture: 7 balanced groups (max 6 tabs each).
 * Tab ids are FROZEN — commandParity.json and ?tab= deep links reference them;
 * only the grouping/order here may change (the parity guard reads the `Tab`
 * union above, never this structure).
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Overview',
    items: [
      { id: 'home', label: 'Home', shortcut: 'h' },
      { id: 'analytics', label: 'Analytics', shortcut: 'a' },
      { id: 'cost', label: 'Cost', badge: 'cost', shortcut: 'x' },
    ],
  },
  {
    label: 'Runs & Debug',
    items: [
      { id: 'runs', label: 'Runs', shortcut: 'r' },
      { id: 'diff', label: 'Diff', shortcut: 'd' },
      { id: 'capture', label: 'Capture', badge: 'L·C', shortcut: 't' },
    ],
  },
  {
    label: 'Govern & Promote',
    items: [
      { id: 'registry', label: 'Registry', shortcut: 'g' },
      { id: 'governance', label: 'Governance', badge: 'Gov', shortcut: 'v' },
      { id: 'eval', label: 'Eval', badge: 'eval', shortcut: 'e' },
      { id: 'risk', label: 'Risk', badge: 'risk', shortcut: 'k' },
      { id: 'policy', label: 'Policy', shortcut: 'p' },
      { id: 'holds', label: 'Holds', shortcut: 'z' },
    ],
  },
  {
    label: 'Provenance & Trust',
    items: [
      { id: 'lineage', label: 'Lineage', shortcut: 'l' },
      { id: 'kg', label: 'KG', badge: 'KG', shortcut: 'n' },
      { id: 'evidence', label: 'Evidence', shortcut: 'w' },
      { id: 'seal', label: 'Seal', badge: 'SoD', shortcut: 's' },
      { id: 'spine', label: 'Spine', badge: 'D3', shortcut: 'y' },
      { id: 'audit', label: 'Audit', shortcut: 'u' },
    ],
  },
  {
    label: 'Compliance',
    items: [
      { id: 'compliance', label: 'Compliance', badge: 'Reg', shortcut: 'c' },
      { id: 'incidents', label: 'Incidents', badge: 'Art73', shortcut: 'i' },
      { id: 'schema', label: 'Schema', badge: 'spec', shortcut: 'm' },
    ],
  },
  {
    label: 'Platform',
    items: [
      { id: 'infra', label: 'Infra', shortcut: 'f' },
      { id: 'storage', label: 'Storage', badge: 'WORM', shortcut: 'b' },
      { id: 'ops', label: 'Ops', shortcut: 'o' },
      { id: 'alerts', label: 'Alerts', badge: 'ops', shortcut: 'j' },
      { id: 'admin', label: 'Admin', shortcut: 'q' },
      { id: 'commands', label: 'Commands', badge: 'CLI', shortcut: '1' },
    ],
  },
  {
    label: 'Reports & Export',
    items: [
      { id: 'reports', label: 'Reports', shortcut: '2' },
      { id: 'export', label: 'Export', badge: 'hub', shortcut: '3' },
    ],
  },
];

export const ALL_TABS: Tab[] = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.id));

/** `g`-sequence second key → tab. Stable across reorders (unlike the old positional 1–9). */
export const SHORTCUT_TAB: Record<string, Tab> = Object.fromEntries(
  NAV_GROUPS.flatMap((g) => g.items.map((i) => [i.shortcut, i.id])),
) as Record<string, Tab>;

/** Per-tab shortcut display string, e.g. 'g h'. */
export const TAB_SHORTCUT: Partial<Record<Tab, string>> = Object.fromEntries(
  NAV_GROUPS.flatMap((g) => g.items.map((i) => [i.id, `g ${i.shortcut}`])),
);

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
    // Default: every group collapsed EXCEPT the one holding the tab we open on,
    // so the current location is visible on first load. 29 tabs expanded at once
    // is a wall of links; collapsed groups make the seven areas the thing you
    // scan first.
    //
    // This is a *seed*, not a rule — every group, including the active one,
    // stays freely collapsible afterwards. Forcing the active group open at
    // render time is what turned its header into a dead button.
    const initialDefault = () =>
      new Set(
        NAV_GROUPS.filter((g) => !g.items.some((i) => i.id === tab)).map((g) => g.label),
      );
    try {
      const raw = localStorage.getItem('novafabric.sidebar-groups');
      return raw ? new Set(JSON.parse(raw) as string[]) : initialDefault();
    } catch {
      return initialDefault();
    }
  });

  /**
   * The group the sidebar opened *for* you, rather than one you opened
   * yourself. Only this one is auto-managed: it gets closed again when you
   * navigate out of it, which is what keeps the panel tidy. A group you opened
   * by hand is yours and is never auto-closed.
   */
  const autoOpenedRef = useRef<string | null>(
    NAV_GROUPS.find((g) => g.items.some((i) => i.id === tab))?.label ?? null,
  );

  // Navigation reveals. You can reach a tab without touching the sidebar — a
  // `g x` shortcut, the command palette, a ?tab= deep link — and if its group
  // were closed the sidebar would highlight nothing at all. So arriving at a
  // tab opens its group, and releases the previously auto-opened one.
  //
  // Keyed on `tab` alone, deliberately: it runs only when the location actually
  // changes, so collapsing the group you are already in stays collapsed instead
  // of springing back open.
  useEffect(() => {
    const owner = NAV_GROUPS.find((g) => g.items.some((i) => i.id === tab));
    if (!owner) return;
    const prevAuto = autoOpenedRef.current;
    if (prevAuto === owner.label) return;
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (prevAuto) next.add(prevAuto);
      next.delete(owner.label);
      try { localStorage.setItem('novafabric.sidebar-groups', JSON.stringify([...next])); } catch { /* ignore */ }
      return next;
    });
    autoOpenedRef.current = owner.label;
  }, [tab]);

  const toggleGroup = (label: string) => {
    // Touching a group by hand takes it out of the sidebar's control.
    if (autoOpenedRef.current === label) autoOpenedRef.current = null;
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
        'flex flex-col shrink-0 border-r border-[var(--color-border)] bg-[var(--color-bg-sunken)] transition-[width] duration-[var(--duration)] overflow-hidden',
        // w-60 (15rem), not w-52 (13rem): at 13rem a row's badge/count sat flush
        // against the hover shortcut hint with nothing between them, and long
        // labels ran to the very edge. The extra 2rem buys real breathing room.
        collapsed ? 'w-11' : 'w-60',
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
          <span className="text-2xs font-medium uppercase tracking-widest text-[var(--color-text-faint)] font-mono select-none">
            NovaFabric
          </span>
        )}
        <button
          onClick={onToggleCollapse}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className="w-6 h-6 flex items-center justify-center rounded text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] transition-colors shrink-0"
        >
          <Icon name={collapsed ? 'expand' : 'collapse'} size={13} />
        </button>
      </div>

      {/* Nav items */}
      <nav className="flex-1 overflow-y-auto py-1" role="navigation">
        {NAV_GROUPS.map((group) => {
          const groupHasActive = group.items.some(i => i.id === tab);
          // Every group collapses, including the one holding the active tab —
          // pinning it open made its header a dead button (you could open a
          // group but never close it). The current location stays discoverable
          // via the marker on the collapsed header instead.
          const groupCollapsed = !collapsed && collapsedGroups.has(group.label);
          return (
            <div key={group.label}>
              {!collapsed && (
                <button
                  type="button"
                  onClick={() => toggleGroup(group.label)}
                  aria-expanded={!groupCollapsed}
                  className="w-full flex items-center justify-between px-3 pt-2.5 pb-0.5 text-2xs font-medium uppercase tracking-wider text-[var(--color-text-faint)] select-none hover:text-[var(--color-text-muted)] transition-colors"
                >
                  <span className="truncate">{group.label}</span>
                  <span aria-hidden="true" className="flex items-center gap-1 font-mono normal-case tracking-normal">
                    {/* "You are here" when the active tab's group is closed. */}
                    {groupCollapsed && groupHasActive && (
                      <span
                        data-testid="group-active-marker"
                        title="Contains the current tab"
                        className="w-1.5 h-1.5 rounded-full bg-[var(--color-accent)] shrink-0"
                      />
                    )}
                    {groupCollapsed && group.items.length}
                    <Icon name="chevron-down" size={10} className={clsx('transition-transform duration-[var(--duration-fast)]', groupCollapsed && '-rotate-90')} />
                  </span>
                </button>
              )}
              {!groupCollapsed && group.items.map((item) => {
                const active = tab === item.id;
                const count = counts[item.id];
                const shortcut = `g ${item.shortcut}`;
                return (
                  <button
                    key={item.id}
                    onClick={() => onTabChange(item.id)}
                    title={collapsed ? `${item.label} (${shortcut})` : `${item.label} — press ${shortcut}`}
                    aria-current={active ? 'page' : undefined}
                    className={clsx(
                      'relative w-full flex items-center gap-2.5 px-3 py-2 text-xs transition-colors group',
                      active
                        ? 'text-[var(--color-text)] bg-[var(--color-bg-raised)]'
                        : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)]',
                    )}
                  >
                    {active && (
                      <span
                        aria-hidden="true"
                        className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r bg-[var(--color-accent)]"
                      />
                    )}
                    <span
                      aria-hidden="true"
                      className={clsx(
                        'w-4 flex items-center justify-center shrink-0',
                        active ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-faint)]',
                      )}
                    >
                      <Icon name={item.id} size={14} />
                    </span>
                    {!collapsed && (
                      <>
                        <span className="flex-1 text-left font-medium truncate">{item.label}</span>
                        {/* The shortcut hint below is absolutely positioned at
                            right-2 and takes no layout space, so this badge/count
                            slot must stay clear of it. At the old w-52 it did not:
                            measured 14px of hint-over-badge overlap on every row.
                            The w-60 width is what keeps them apart —
                            tests/e2e/dashboard-sidebar.spec.ts fails if that
                            clearance is ever lost again. */}
                        {item.badge && <Badge className="shrink-0">{item.badge}</Badge>}
                        {count !== undefined && !item.badge && (
                          <span className={clsx(
                            'text-2xs font-mono shrink-0 tabular-nums',
                            active ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-faint)]',
                          )}>
                            {count}
                          </span>
                        )}
                        <span
                          aria-hidden="true"
                          className="absolute right-2 text-2xs text-[var(--color-text-faint)] opacity-0 group-hover:opacity-60 font-mono transition-opacity"
                        >
                          {shortcut}
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
              'text-2xs font-mono transition-colors',
              autoRefresh ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-faint)]',
            )}>
              auto {autoRefresh && '·30s'}
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
              <code className="text-2xs font-mono text-[var(--color-text-faint)] truncate">{serverInfo.base}</code>
            </div>
            {/* min-w-0 + shrink: the sidebar is 13rem, so a long version string
                must not push the badge past the edge (it was clipping). */}
            <div className="pl-3 flex items-center gap-2 min-w-0">
              <span className="text-2xs font-mono text-[var(--color-text-faint)] truncate">
                v{serverInfo.version}
              </span>
              <Badge tone="info" className="shrink-0">exp</Badge>
            </div>
          </div>
        )}

        {/* Help */}
        <button
          onClick={onHelpOpen}
          title="Keyboard shortcuts (?)"
          className={clsx(
            'w-full flex items-center gap-2 px-1 py-1 rounded text-2xs text-[var(--color-text-faint)] hover:text-[var(--color-text-muted)] transition-colors',
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
            'w-full flex items-center gap-2 px-1 py-1 rounded text-2xs transition-colors',
            showAppearance
              ? 'text-[var(--color-text)] bg-[var(--color-accent-tint)]'
              : 'text-[var(--color-text-faint)] hover:text-[var(--color-text-muted)]',
            collapsed ? 'justify-center' : '',
          )}
        >
          <Icon name="settings" size={12} />
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
            'w-full flex items-center gap-2 px-1 py-1 rounded text-2xs text-[var(--color-text-faint)] hover:text-[var(--color-status-failure)] hover:bg-[var(--color-danger-tint)] transition-colors',
            collapsed ? 'justify-center' : '',
          )}
        >
          <Icon name="disconnect" size={12} />
          {!collapsed && <span>Disconnect</span>}
        </button>
      </div>
    </aside>
  );
}
