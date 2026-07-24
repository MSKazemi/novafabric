import { useEffect, useState, useCallback, useMemo, lazy, Suspense } from 'react';
import { api, getConnection, clearConnection, ping, validateToken, onAuthChange, setConnection } from '../../lib/api';

function openTopology() {
  const { token, base } = getConnection();
  const origin = base || window.location.origin;
  const url = `${origin}/topology/${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  window.open(url, '_blank', 'noopener,noreferrer');
}
import Sidebar, { type Tab, ALL_TABS, NAV_GROUPS } from './Sidebar';
import ConnectPanel from './ConnectPanel';
import KeyboardHelp from '../ui/KeyboardHelp';
import ErrorBoundary from '../ui/ErrorBoundary';
import CommandPalette, { type Command } from '../ui/CommandPalette';
import { Loading } from './helpers';
import { usePolling } from '../../lib/usePolling';
import { ToastProvider, useToast } from '../../lib/ToastContext';

// Tabs are code-split: each becomes its own chunk, loaded on first navigation
// instead of shipping all ~20 in the initial bundle.
const AnalyticsTab = lazy(() => import('./tabs/AnalyticsTab'));
const AlertsTab = lazy(() => import('./tabs/AlertsTab'));
const RunsTab = lazy(() => import('./tabs/RunsTab'));
const RegistryTab = lazy(() => import('./tabs/RegistryTab'));
const LineageTab = lazy(() => import('./tabs/LineageTab'));
const DiffTab = lazy(() => import('./tabs/DiffTab'));
const CaptureTab = lazy(() => import('./tabs/CaptureTab'));
const AuditTab = lazy(() => import('./tabs/AuditTab'));
const HomeTab = lazy(() => import('./tabs/HomeTab'));
const CommandsTab = lazy(() => import('./tabs/CommandsTab'));
const EvidenceTab = lazy(() => import('./tabs/EvidenceTab'));
const HoldsTab = lazy(() => import('./tabs/HoldsTab'));
const PolicyTab = lazy(() => import('./tabs/PolicyTab'));
const SealTab = lazy(() => import('./tabs/SealTab'));
const InfraTab = lazy(() => import('./tabs/InfraTab'));
const AdminTab = lazy(() => import('./tabs/AdminTab'));
const ComplianceTab = lazy(() => import('./tabs/ComplianceTab'));
const GovernanceTab = lazy(() => import('./tabs/GovernanceTab'));
const KGTab = lazy(() => import('./tabs/KGTab'));
const CostTab = lazy(() => import('./tabs/CostTab'));
const SchemaTab = lazy(() => import('./tabs/SchemaTab'));
const ReportsTab = lazy(() => import('./tabs/ReportsTab'));
const ExportCenter = lazy(() => import('./ExportCenter'));
const EvalTab = lazy(() => import('./tabs/EvalTab'));
const RiskTab = lazy(() => import('./tabs/RiskTab'));
const StorageTab = lazy(() => import('./tabs/StorageTab'));
const IncidentsTab = lazy(() => import('./tabs/IncidentsTab'));
const OpsTab = lazy(() => import('./tabs/OpsTab'));
const SpineTab = lazy(() => import('./tabs/SpineTab'));

const TABS: Tab[] = ALL_TABS;

const TAB_LABEL: Record<Tab, string> = Object.fromEntries(
  NAV_GROUPS.flatMap(g => g.items.map(i => [i.id, i.label]))
) as Record<Tab, string>;

function consumeTokenFromUrl(): boolean {
  if (typeof window === 'undefined') return false;
  const params = new URLSearchParams(window.location.search);
  const t = params.get('token');
  if (!t) return false;
  setConnection(t, window.location.origin);
  params.delete('token');
  const cleaned = window.location.pathname + (params.toString() ? `?${params.toString()}` : '') + window.location.hash;
  window.history.replaceState({}, '', cleaned);
  return true;
}

export default function DashboardApp() {
  return (
    <ToastProvider>
      <DashboardInner />
    </ToastProvider>
  );
}

function DashboardInner() {
  const { toast } = useToast();
  // Connection
  const [connected, setConnected] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);
  const [serverInfo, setServerInfo] = useState<{ version: string; base: string } | null>(null);

  // Layout
  const [tab, setTab] = useState<Tab>(() => {
    if (typeof window === 'undefined') return 'home';
    const p = new URLSearchParams(window.location.search);
    if (p.get('run_ids') || (p.get('run_a') && p.get('run_b'))) return 'diff';
    const t = p.get('tab');
    if (t && (ALL_TABS as string[]).includes(t)) return t as Tab;
    return 'home';
  });
  const [diffIds, setDiffIds] = useState<string[]>(() => {
    if (typeof window === 'undefined') return [];
    const p = new URLSearchParams(window.location.search);
    const ids = p.get('run_ids');
    if (ids) return ids.split(',').filter(Boolean);
    const a = p.get('run_a');
    const b = p.get('run_b');
    return a && b ? [a, b] : [];
  });
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('novafabric.sidebar-collapsed') === 'true'; } catch { return false; }
  });
  const [showHelp, setShowHelp] = useState(false);
  const [showPalette, setShowPalette] = useState(false);

  // Refresh
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);

  // Sidebar counts
  const [counts, setCounts] = useState<Partial<Record<Tab, number>>>({});

  // Auto-refresh polling — visibility-aware: pauses while the tab is hidden,
  // refreshes immediately on return (no background full-table query storms).
  usePolling(() => setRefreshTick(t => t + 1), 30000, autoRefresh);

  // Initial connect / token-from-URL
  useEffect(() => {
    (async () => {
      consumeTokenFromUrl();
      const { token, base } = getConnection();
      if (!token) return;
      try {
        await validateToken(base, token);
        const h = await ping(base);
        setServerInfo({ version: h.version, base });
        setConnected(true);
      } catch {
        clearConnection();
        setBootError('Token rejected — get the current token from the terminal: cat ~/.novafabric/.serve-token');
      }
    })();

    const unsubscribe = onAuthChange(() => {
      setConnected(false);
      setServerInfo(null);
      setBootError('Session expired — please reconnect with the current token.');
    });
    return unsubscribe;
  }, []);

  // Populate sidebar counts on connect
  useEffect(() => {
    if (!connected) return;
    Promise.all([
      api.getStats().catch(() => null),
      api.listHolds().catch(() => null),
      api.incidentList().catch(() => null),
    ]).then(([stats, holds, incidents]) => {
      setCounts(prev => ({
        ...prev,
        runs: stats?.run_count,
        registry: stats?.asset_count,
        holds: holds?.total_active ?? undefined,
        incidents: incidents?.incidents?.filter(i => i.status !== 'closed').length,
      }));
    });
  }, [connected]);

  // Keyboard shortcuts
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      // Cmd/Ctrl+K opens the command palette from anywhere, even inside inputs.
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setShowPalette(p => !p);
        return;
      }
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === '?') { setShowHelp(h => !h); return; }
      if (e.key === 'Escape') { setShowHelp(false); return; }
      if (e.key === 'r') { setRefreshTick(t => t + 1); return; }
      const n = parseInt(e.key, 10);
      if (n >= 1 && n <= TABS.length) setTab(TABS[n - 1]);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const handleCompareTo = useCallback((ids: string[]) => {
    setDiffIds(ids);
    setTab('diff');
    try {
      const p = new URLSearchParams(window.location.search);
      p.set('run_ids', ids.join(','));
      p.delete('run_a');
      p.delete('run_b');
      window.history.replaceState({}, '', `${window.location.pathname}?${p.toString()}${window.location.hash}`);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    if (tab !== 'diff') {
      setDiffIds([]);
      try {
        const p = new URLSearchParams(window.location.search);
        if (p.has('run_ids') || p.has('run_a') || p.has('run_b')) {
          p.delete('run_ids');
          p.delete('run_a');
          p.delete('run_b');
          const qs = p.toString();
          window.history.replaceState({}, '', `${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`);
        }
      } catch { /* ignore */ }
    }
  }, [tab]);

  const flash = useCallback((tone: 'success' | 'error', text: string) => {
    toast(tone, text);
  }, [toast]);

  const handleDisconnect = useCallback(() => {
    clearConnection();
    setConnected(false);
    setServerInfo(null);
    setCounts({});
  }, []);

  const handleTabChange = useCallback((t: Tab) => {
    setTab(t);
    // Deep-link the active tab so it is shareable / survives reload.
    try {
      const p = new URLSearchParams(window.location.search);
      if (t === 'home') p.delete('tab'); else p.set('tab', t);
      const qs = p.toString();
      window.history.replaceState({}, '', `${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`);
    } catch { /* ignore */ }
  }, []);

  // Stable per-section callbacks — created once, never recreated, so tabs don't
  // loop when onCountChange is in their useCallback deps.
  const stableCountHandlers = useMemo<Record<Tab, (n: number) => void>>(() => {
    return Object.fromEntries(
      TABS.map(t => [t, (n: number) => setCounts(prev => ({ ...prev, [t]: n }))])
    ) as Record<Tab, (n: number) => void>;
  // setCounts is stable (from useState), TABS is a module-level constant
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const handleCountChange = useCallback((section: Tab) => stableCountHandlers[section], [stableCountHandlers]);

  // Command-palette entries: jump to any view + global actions.
  const paletteCommands = useMemo<Command[]>(() => {
    const navCommands: Command[] = NAV_GROUPS.flatMap(g =>
      g.items.map(i => ({
        id: `tab:${i.id}`,
        label: i.label,
        group: g.label,
        keywords: i.id,
        run: () => setTab(i.id as Tab),
      }))
    );
    const actions: Command[] = [
      { id: 'act:refresh', label: 'Refresh data', group: 'Action', keywords: 'reload', run: () => setRefreshTick(t => t + 1) },
      { id: 'act:help', label: 'Keyboard shortcuts', group: 'Action', keywords: 'help keys', run: () => setShowHelp(true) },
      { id: 'act:topology', label: 'Open Live Topology', group: 'Action', keywords: 'graph network', run: openTopology },
      { id: 'act:disconnect', label: 'Disconnect', group: 'Action', keywords: 'logout sign out', run: handleDisconnect },
    ];
    return [...navCommands, ...actions];
  }, [handleDisconnect]);

  // Live entity search for the palette: jump to any run / asset / incident by id.
  const paletteSearch = useCallback(async (q: string): Promise<Command[]> => {
    const ql = q.toLowerCase();
    const [runs, assets, incidents] = await Promise.all([
      api.searchRuns({ q, limit: 6 }).then(r => r.items).catch(() => []),
      api.listAssets({ limit: 100 }).then(r => r.assets).catch(() => []),
      api.incidentList().then(r => r.incidents).catch(() => []),
    ]);
    const out: Command[] = [];
    for (const r of runs) {
      out.push({
        id: `ent:run:${r.run_id}`,
        label: `Run ${r.run_id}`,
        group: r.status ?? 'run',
        run: () => handleTabChange('runs'),
      });
    }
    for (const a of assets.filter(a => `${a.name} ${a.version}`.toLowerCase().includes(ql)).slice(0, 6)) {
      out.push({
        id: `ent:asset:${a.id}`,
        label: `${a.name} @ ${a.version}`,
        group: 'asset',
        run: () => handleTabChange('registry'),
      });
    }
    for (const i of incidents.filter(i => i.title.toLowerCase().includes(ql)).slice(0, 6)) {
      out.push({
        id: `ent:incident:${i.id}`,
        label: `Incident: ${i.title}`,
        group: 'incident',
        run: () => handleTabChange('incidents'),
      });
    }
    return out;
  }, [handleTabChange]);

  if (!connected) {
    return (
      <ConnectPanel
        onConnected={(info) => { setServerInfo(info); setConnected(true); setBootError(null); }}
        bootError={bootError}
        setBootError={setBootError}
      />
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--color-bg)]">
      <Sidebar
        tab={tab}
        onTabChange={handleTabChange}
        counts={counts}
        serverInfo={serverInfo}
        onDisconnect={handleDisconnect}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed(c => {
          const next = !c;
          try { localStorage.setItem('novafabric.sidebar-collapsed', String(next)); } catch { /* ignore */ }
          return next;
        })}
        autoRefresh={autoRefresh}
        onAutoRefreshChange={setAutoRefresh}
        onHelpOpen={() => setShowHelp(true)}
      />

      <main className="flex-1 overflow-y-auto flex flex-col">
        {/* Breadcrumb top bar */}
        <div className="flex items-center justify-between px-4 py-1.5 border-b border-[var(--color-border)] bg-[var(--color-bg)] shrink-0 gap-4">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-[10px] font-mono text-[var(--color-text-faint)] shrink-0 select-none">NovaFabric</span>
            <span className="text-[var(--color-border-strong)] shrink-0" aria-hidden="true">/</span>
            <span className="text-xs font-semibold text-[var(--color-text)] truncate">{TAB_LABEL[tab] ?? tab}</span>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            {serverInfo && (
              <span className="flex items-center gap-1 text-[10px] font-mono text-[var(--color-text-faint)]">
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-status-success)] shrink-0" aria-hidden="true" />
                connected
              </span>
            )}
            <button
              onClick={openTopology}
              title="Open Live Topology view in a new tab"
              className="flex items-center gap-1.5 px-2.5 py-1 rounded border border-[var(--color-border)] text-[10px] font-mono text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_8%,transparent)] transition-colors"
            >
              <span aria-hidden="true">⬡</span>
              <span>Topology ↗</span>
            </button>
          </div>
        </div>
        <div className="p-4 flex-1">
          {/* key={tab} remounts a fresh boundary + suspense on each navigation,
              so a crashed/loading tab never blocks switching away. */}
          <ErrorBoundary key={tab} label={`The ${TAB_LABEL[tab] ?? tab} view hit an error.`}>
            <Suspense fallback={<Loading />}>
          {tab === 'home' && (
            <HomeTab onNavigate={handleTabChange} />
          )}
          {tab === 'analytics' && <AnalyticsTab />}
          {tab === 'runs' && (
            <RunsTab
              onFlash={flash}
              refreshTick={refreshTick}
              onCountChange={handleCountChange('runs')}
              onNavigate={handleTabChange}
              onCompareTo={handleCompareTo}
            />
          )}
          {tab === 'registry' && (
            <RegistryTab
              onFlash={flash}
              refreshTick={refreshTick}
              onCountChange={handleCountChange('registry')}
              onNavigate={handleTabChange}
            />
          )}
          {tab === 'lineage' && <LineageTab refreshTick={refreshTick} onCountChange={handleCountChange('lineage')} />}
          {tab === 'diff' && <DiffTab initialIds={diffIds.length > 0 ? diffIds : undefined} />}
          {tab === 'capture' && <CaptureTab />}
          {tab === 'evidence' && <EvidenceTab onNavigate={handleTabChange} onCountChange={handleCountChange('evidence')} />}
          {tab === 'audit' && (
            <AuditTab
              refreshTick={refreshTick}
              onCountChange={handleCountChange('audit')}
            />
          )}
          {tab === 'holds' && (
            <HoldsTab
              refreshTick={refreshTick}
              onCountChange={handleCountChange('holds')}
            />
          )}
          {tab === 'policy' && <PolicyTab />}
          {tab === 'seal' && <SealTab />}
          {tab === 'governance' && <GovernanceTab />}
          {tab === 'kg' && <KGTab />}
          {tab === 'cost' && <CostTab />}
          {tab === 'schema' && <SchemaTab />}
          {tab === 'compliance' && <ComplianceTab />}
          {tab === 'infra' && <InfraTab />}
          {tab === 'commands' && <CommandsTab />}
          {tab === 'admin' && <AdminTab />}
          {tab === 'reports' && <ReportsTab />}
          {tab === 'export' && <ExportCenter onNavigate={handleTabChange} />}
          {tab === 'eval' && <EvalTab />}
          {tab === 'risk' && <RiskTab onNavigate={handleTabChange} />}
          {tab === 'storage' && <StorageTab />}
          {tab === 'incidents' && <IncidentsTab onCountChange={handleCountChange('incidents')} />}
          {tab === 'spine' && <SpineTab onNavigate={handleTabChange} />}
          {tab === 'ops' && <OpsTab />}
          {tab === 'alerts' && <AlertsTab />}
            </Suspense>
          </ErrorBoundary>
        </div>
      </main>

      {/* Command palette (Cmd/Ctrl+K) */}
      {showPalette && (
        <CommandPalette commands={paletteCommands} onClose={() => setShowPalette(false)} search={paletteSearch} />
      )}

      {/* Keyboard help overlay */}
      {showHelp && (
        <KeyboardHelp
          onClose={() => setShowHelp(false)}
          tabLabels={NAV_GROUPS.flatMap(g => g.items.map(i => i.label))}
        />
      )}
    </div>
  );
}
