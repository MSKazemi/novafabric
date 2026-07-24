import { useState, useEffect, useCallback } from 'react';
import { clsx } from 'clsx';
import { api, getConnection, type RunSummary } from '../../../lib/api';
import { relativeTime } from '../../../lib/time';
import { usePolling } from '../../../lib/usePolling';
import type { Tab } from '../Sidebar';

// ── System Health types ───────────────────────────────────────────────

interface SystemHealth {
  backend_type: 'sqlite' | 'postgres';
  db_path: string;
  keystore_ok: boolean;
  opa_available: boolean;
  novaseal_configured: boolean;
  schema_version: string;
}

// ── Types ─────────────────────────────────────────────────────────────

interface ResumeItem {
  tab: Tab;
  label: string;
  meta: string;
  icon: string;
  ts?: string;
}

interface HomeCounts {
  totalRuns: number;
  failedRuns: number;
  passedRuns: number;
  totalAssets: number;
  pendingEval: number;
  productionAssets: number;
}

// ── localStorage helpers ──────────────────────────────────────────────

function readResume(): ResumeItem[] {
  try {
    const raw = localStorage.getItem('novafabric.resume-items');
    return raw ? (JSON.parse(raw) as ResumeItem[]) : [];
  } catch { return []; }
}

export function writeResumeItem(item: ResumeItem): void {
  try {
    const existing = readResume().filter((r) => r.tab !== item.tab || r.label !== item.label);
    const updated = [{ ...item, ts: new Date().toISOString() }, ...existing].slice(0, 3);
    localStorage.setItem('novafabric.resume-items', JSON.stringify(updated));
  } catch { /* ignore */ }
}

// ── Sub-components ────────────────────────────────────────────────────

function StatusPill({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className="flex items-center gap-1.5 text-[11px] text-[var(--color-text-faint)]">
      <span className={clsx(
        'w-1.5 h-1.5 rounded-full shrink-0',
        ok ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-failure)]',
      )} aria-hidden="true" />
      {label}
    </span>
  );
}

function JourneyCard({
  icon, title, description, stats, cta, onClick,
  accentClass,
}: {
  icon: string;
  title: string;
  description: string;
  stats: Array<{ value: number | string; label: string; tone?: 'normal' | 'failure' | 'success' | 'pending' }>;
  cta: string;
  onClick: () => void;
  accentClass: string;
}) {
  const toneClass: Record<string, string> = {
    normal: 'text-[var(--color-text)]',
    failure: 'text-[var(--color-status-failure)]',
    success: 'text-[var(--color-status-success)]',
    pending: 'text-[var(--color-status-pending)]',
  };
  return (
    <button
      onClick={onClick}
      className="text-left rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 hover:border-[var(--color-accent)] transition-colors group relative overflow-hidden"
    >
      <span aria-hidden="true" className={clsx('absolute top-0 left-0 right-0 h-0.5', accentClass)} />
      <div className="text-xl mb-2">{icon}</div>
      <h3 className="text-sm font-semibold text-[var(--color-text)] mb-1">{title}</h3>
      <p className="text-xs text-[var(--color-text-faint)] leading-relaxed mb-3">{description}</p>
      <div className="flex gap-4 pt-3 border-t border-[var(--color-border)]">
        {stats.map((s) => (
          <div key={s.label}>
            <div className={clsx('text-base font-bold tabular-nums', toneClass[s.tone ?? 'normal'])}>
              {s.value}
            </div>
            <div className="text-[10px] text-[var(--color-text-faint)]">{s.label}</div>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[11px] text-[var(--color-accent)] group-hover:underline">
        → {cta}
      </div>
    </button>
  );
}

function ActivityRow({ run }: { run: RunSummary }) {
  const ok = run.status === 'success';
  const relTime = run.created_at
    ? (() => {
        const ms = Date.now() - new Date(run.created_at).getTime();
        const min = Math.floor(ms / 60000);
        if (min < 1) return 'just now';
        if (min < 60) return `${min} min ago`;
        return `${Math.floor(min / 60)} hr ago`;
      })()
    : '—';

  return (
    <div className="flex items-center gap-2.5 px-4 py-2.5 border-b border-[var(--color-border)] last:border-0 text-xs">
      <span
        aria-hidden="true"
        className={clsx(
          'w-1.5 h-1.5 rounded-full shrink-0',
          ok ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-failure)]',
        )}
      />
      <span className="flex-1 font-mono text-[var(--color-text-muted)] truncate">
        {run.command.join(' ')}
      </span>
      <span className="shrink-0 text-[var(--color-text-faint)]">{relTime}</span>
      <span className={clsx(
        'shrink-0 text-[9px] uppercase tracking-wider px-1.5 py-px rounded',
        ok
          ? 'bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] text-[var(--color-status-success)]'
          : 'bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] text-[var(--color-status-failure)]',
      )}>
        {run.status ?? '—'}
      </span>
    </div>
  );
}

// ── System Health Card ────────────────────────────────────────────────

function SystemHealthCard({ health }: { health: SystemHealth | null }) {
  if (!health) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
        <div className="px-4 py-2.5 border-b border-[var(--color-border)]">
          <h3 className="text-xs font-semibold text-[var(--color-text-muted)]">System Health</h3>
        </div>
        <p className="px-4 py-4 text-xs text-[var(--color-text-faint)]">Loading…</p>
      </div>
    );
  }

  const hasAmber = !health.opa_available || !health.novaseal_configured;
  const truncatedPath = health.db_path.length > 40
    ? `…${health.db_path.slice(-40)}`
    : health.db_path;

  const borderClass = hasAmber
    ? 'border-[color-mix(in_oklab,var(--color-status-pending)_35%,var(--color-border))]'
    : 'border-[var(--color-border)]';

  return (
    <div className={clsx('rounded-lg border bg-[var(--color-bg-raised)] overflow-hidden', borderClass)}>
      <div className="px-4 py-2.5 border-b border-[var(--color-border)] flex items-center justify-between">
        <h3 className="text-xs font-semibold text-[var(--color-text-muted)]">System Health</h3>
        <span className="text-[10px] font-mono text-[var(--color-text-faint)]">schema {health.schema_version}</span>
      </div>
      <div className="px-4 py-3 space-y-2">
        {/* Backend + DB path row */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-[var(--color-text-faint)]">Backend:</span>
            <span className="text-[10px] font-mono font-medium text-[var(--color-text)]">{health.backend_type}</span>
          </div>
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-[10px] text-[var(--color-text-faint)]">DB:</span>
            <span className="text-[10px] font-mono text-[var(--color-text-muted)] truncate" title={health.db_path}>{truncatedPath}</span>
          </div>
        </div>

        {/* Status row */}
        <div className="flex flex-wrap gap-3">
          {/* Key store */}
          <div className="flex items-center gap-1.5">
            <span className={clsx(
              'w-1.5 h-1.5 rounded-full shrink-0',
              health.keystore_ok ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-failure)]',
            )} aria-hidden="true" />
            <span className="text-[10px] text-[var(--color-text-faint)]">
              Key store: {health.keystore_ok ? 'found' : 'not found'}
            </span>
          </div>

          {/* OPA — amber if missing (optional) */}
          <div className="flex items-center gap-1.5">
            <span className={clsx(
              'w-1.5 h-1.5 rounded-full shrink-0',
              health.opa_available ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-pending)]',
            )} aria-hidden="true" />
            <span className={clsx(
              'text-[10px]',
              health.opa_available ? 'text-[var(--color-text-faint)]' : 'text-[var(--color-status-pending)]',
            )}>
              OPA: {health.opa_available ? 'found' : 'not found'}
            </span>
          </div>

          {/* NovaSeal — amber if not configured (optional) */}
          <div className="flex items-center gap-1.5">
            <span className={clsx(
              'w-1.5 h-1.5 rounded-full shrink-0',
              health.novaseal_configured ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-pending)]',
            )} aria-hidden="true" />
            <span className={clsx(
              'text-[10px]',
              health.novaseal_configured ? 'text-[var(--color-text-faint)]' : 'text-[var(--color-status-pending)]',
            )}>
              NovaSeal: {health.novaseal_configured ? 'configured' : 'not configured'}
            </span>
          </div>
        </div>

        {/* Hint */}
        <p className="text-[9px] text-[var(--color-text-faint)] font-mono pt-1">
          Run <code className="px-1 py-px rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)]">nova doctor</code> for full diagnostics
        </p>
      </div>
    </div>
  );
}

// ── Staleness helpers ─────────────────────────────────────────────────

const STALE_MS = 24 * 60 * 60 * 1000;
const isStale = (ts: string | undefined) => ts ? Date.now() - new Date(ts).getTime() > STALE_MS : false;

// ── Main component ────────────────────────────────────────────────────

export default function HomeTab({ onNavigate }: { onNavigate: (tab: Tab) => void }) {
  const [counts, setCounts] = useState<HomeCounts | null>(null);
  const [recentRuns, setRecentRuns] = useState<RunSummary[]>([]);
  const [resumeItems] = useState<ResumeItem[]>(readResume);
  const [error, setError] = useState<string | null>(null);
  const [evidenceCount, setEvidenceCount] = useState<{ total: number; verified: number } | null>(null);
  const [systemHealth, setSystemHealth] = useState<SystemHealth | null>(null);
  const [costSummary, setCostSummary] = useState<{ cost_usd: number; calls: number; available: boolean } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [statsRes, runsRes] = await Promise.all([
        api.getStats(),
        api.listRuns({ limit: 10 }),
      ]);
      setCounts({
        totalRuns: statsRes.run_count,
        failedRuns: statsRes.failed_run_count,
        passedRuns: statsRes.passed_run_count,
        totalAssets: statsRes.asset_count,
        pendingEval: statsRes.pending_eval_count,
        productionAssets: statsRes.production_asset_count,
      });
      setRecentRuns(runsRes.runs);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Initial load on mount; recurring poll pauses while the tab is hidden.
  useEffect(() => { load(); }, [load]);
  usePolling(load, 30000);

  // One-time evidence count fetch on mount (no polling — bundles are immutable)
  useEffect(() => {
    api.listEvidence()
      .then((data) => {
        setEvidenceCount({
          // Use the server's true total (accurate even when the bounded page
          // returned fewer bundles than exist on disk — S3 scale slice).
          total: data.total ?? data.count,
          verified: data.bundles.filter((b) => b.verified).length,
        });
      })
      .catch(() => { /* leave null — card shows '—' */ });
  }, []);

  // 7-day cost summary from ClickHouse (graceful no-op when not configured)
  useEffect(() => {
    const { token, base } = getConnection();
    if (!token) return;
    fetch(`${base}/api/cost/report?days=7&token=${encodeURIComponent(token)}`)
      .then(r => r.ok ? r.json() : Promise.reject())
      .then((data: { ok?: boolean; totals?: { cost_usd: number }; by_model?: Array<{ calls: number }> }) => {
        if (data.ok) {
          const calls = (data.by_model ?? []).reduce((s, m) => s + (m.calls ?? 0), 0);
          setCostSummary({ cost_usd: data.totals?.cost_usd ?? 0, calls, available: true });
        }
      })
      .catch(() => { /* ClickHouse not configured — leave null */ });
  }, []);

  // One-time system health fetch (unauthenticated — uses ping, not api.*)
  useEffect(() => {
    const { base } = (() => {
      try { return { base: localStorage.getItem('novafabric.serve-base') ?? 'http://127.0.0.1:4321' }; }
      catch { return { base: 'http://127.0.0.1:4321' }; }
    })();
    fetch(`${base}/api/health`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error('health failed')))
      .then((data: SystemHealth & Record<string, unknown>) => setSystemHealth(data as SystemHealth))
      .catch(() => { /* leave null */ });
  }, []);

  return (
    <div className="space-y-4">
      {/* Status bar */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 px-1 py-2 text-xs border-b border-[var(--color-border)]">
        {counts ? (
          <>
            <StatusPill ok={counts.failedRuns === 0} label={counts.failedRuns === 0 ? 'All runs passing' : `${counts.failedRuns} failed run${counts.failedRuns === 1 ? '' : 's'}`} />
            <StatusPill ok={counts.pendingEval === 0} label={counts.pendingEval === 0 ? 'No pending evals' : `${counts.pendingEval} pending eval`} />
            <StatusPill ok label="Registry OK" />
          </>
        ) : (
          <span className="text-[var(--color-text-faint)]">Loading status…</span>
        )}
        <span className="ml-auto flex items-center gap-4">
          {error && <span className="text-[var(--color-status-failure)]">{error}</span>}
          <span className="text-[var(--color-text-faint)]">auto-refresh 30s</span>
        </span>
      </div>

      {/* Journey cards */}
      <div className={clsx('grid gap-3', costSummary?.available ? 'sm:grid-cols-4' : 'sm:grid-cols-3')}>
        <JourneyCard
          icon="🔍"
          title="Debug & Investigate"
          description="Inspect runs, trace waterfalls, diff against a known-good capsule, replay forensically."
          stats={[
            { value: counts?.totalRuns ?? '—', label: 'total runs' },
            { value: counts?.failedRuns ?? '—', label: 'failed', tone: (counts?.failedRuns ?? 0) > 0 ? 'failure' : 'normal' },
            { value: counts?.passedRuns ?? '—', label: 'passed', tone: 'success' },
          ]}
          cta="Go to Runs"
          onClick={() => onNavigate('runs')}
          accentClass="bg-gradient-to-r from-[var(--color-accent)] to-blue-500"
        />
        <JourneyCard
          icon="🏛"
          title="Govern & Promote"
          description="Manage the asset registry, run eval suites, gate promotions, track eval trends."
          stats={[
            { value: counts?.totalAssets ?? '—', label: 'assets' },
            { value: counts?.pendingEval ?? '—', label: 'pending eval', tone: (counts?.pendingEval ?? 0) > 0 ? 'pending' : 'normal' },
            { value: counts?.productionAssets ?? '—', label: 'production', tone: 'success' },
          ]}
          cta="Go to Registry"
          onClick={() => onNavigate('registry')}
          accentClass="bg-gradient-to-r from-cyan-500 to-teal-500"
        />
        <JourneyCard
          icon="🔐"
          title="Audit & Verify"
          description="Walk the provenance chain, verify evidence bundles, export signed reports for compliance."
          stats={[
            { value: evidenceCount !== null ? evidenceCount.total : '—', label: 'evidence bundles' },
            { value: evidenceCount !== null ? evidenceCount.verified : '—', label: 'verified', tone: 'success' },
            { value: '0', label: 'violations', tone: 'success' },
          ]}
          cta="Go to Evidence"
          onClick={() => onNavigate('evidence')}
          accentClass="bg-gradient-to-r from-amber-600 to-orange-700"
        />
        {costSummary?.available && (
          <JourneyCard
            icon="💰"
            title="Cost & Usage"
            description="LLM token spend, model call counts, and per-run cost breakdown — last 7 days."
            stats={[
              { value: costSummary.calls, label: 'model calls' },
              { value: `$${costSummary.cost_usd.toFixed(4)}`, label: '7d spend' },
            ]}
            cta="Go to Cost"
            onClick={() => onNavigate('cost')}
            accentClass="bg-gradient-to-r from-green-600 to-emerald-500"
          />
        )}
      </div>

      {/* Bottom row: activity + resume */}
      <div className="grid gap-3 lg:grid-cols-2">
        {/* Recent activity */}
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-[var(--color-border)]">
            <h3 className="text-xs font-semibold text-[var(--color-text-muted)]">Recent Activity</h3>
            <button
              onClick={() => onNavigate('runs')}
              className="text-[10px] text-[var(--color-accent)] hover:underline"
            >
              View all →
            </button>
          </div>
          {recentRuns.length > 0
            ? recentRuns.map((r) => <ActivityRow key={r.run_id} run={r} />)
            : <p className="px-4 py-6 text-xs text-[var(--color-text-faint)]">No runs yet.</p>
          }
        </div>

        {/* Resume last session */}
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
          <div className="px-4 py-2.5 border-b border-[var(--color-border)]">
            <h3 className="text-xs font-semibold text-[var(--color-text-muted)]">Resume Last Session</h3>
          </div>
          <div className="p-3 space-y-2">
            {resumeItems.length > 0
              ? resumeItems.map((item) => (
                  <button
                    key={`${item.tab}:${item.label}`}
                    onClick={() => onNavigate(item.tab)}
                    title={isStale(item.ts) ? `Last updated ${relativeTime(item.ts!)} — may be stale` : ''}
                    className={clsx(
                      'w-full flex items-center gap-3 p-2.5 rounded border bg-[var(--color-bg-sunken)] hover:border-[var(--color-accent)] transition-colors text-left',
                      isStale(item.ts) ? 'border-[var(--color-status-pending)]' : 'border-[var(--color-border)]',
                    )}
                  >
                    <span aria-hidden="true" className="text-base shrink-0">{item.icon}</span>
                    <div className="flex-1 min-w-0">
                      <div className="text-xs font-medium text-[var(--color-text-muted)] truncate">{item.label}</div>
                      <div className="text-[10px] text-[var(--color-text-faint)] truncate">{item.meta}</div>
                    </div>
                    <span aria-hidden="true" className="text-[var(--color-text-faint)] shrink-0">→</span>
                  </button>
                ))
              : <p className="px-1 py-4 text-xs text-[var(--color-text-faint)]">Nothing to resume yet — open a run, diff, or asset to start tracking.</p>
            }
          </div>
        </div>
      </div>

      {/* System Health card (DD-5) */}
      <SystemHealthCard health={systemHealth} />
    </div>
  );
}
