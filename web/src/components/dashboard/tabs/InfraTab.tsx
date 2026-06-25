
import { useCallback, useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import { usePolling } from '../../../lib/usePolling';
import { SuggestInput } from '../../ui/SuggestInput';
import CopyButton from '../../ui/CopyButton';

type StatusBadge = 'shipped' | 'partial' | 'placeholder' | 'planned';

interface ComponentCard {
  title: string;
  phase: string;
  badge: StatusBadge;
  description: string;
  shipped: string[];
  dashboardUi: string | null;
  cliCommands: string[];
  note?: string;
}

const BADGE_LABEL: Record<StatusBadge, string> = {
  shipped: 'shipped',
  partial: 'partial UI',
  placeholder: 'CLI only',
  planned: 'planned',
};

const BADGE_COLOR: Record<StatusBadge, string> = {
  shipped: 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)]',
  partial: 'text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)] border-[color-mix(in_oklab,var(--color-accent)_30%,transparent)]',
  placeholder: 'text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_12%,transparent)] border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)]',
  planned: 'text-[var(--color-text-faint)] bg-[var(--color-bg-sunken)] border-[var(--color-border)]',
};

const COMPONENTS: ComponentCard[] = [
  {
    title: 'Run Capsule + Capture',
    phase: 'v0.2',
    badge: 'shipped',
    description: 'Core capture pipeline — wraps any Python command and records model calls, tool calls, trace events, environment snapshot, and exit code as a portable Run Capsule.',
    shipped: [
      'nova capture (local, Docker, Kubernetes, Slurm runners)',
      'nova mcp-proxy — MCP stdio wire-level capture',
      'nova api-proxy — non-Python LLM client capture (HTTP proxy)',
      'OTel GenAI semconv full coverage (v0.6)',
      'aiohttp, urllib3, Bedrock wire-level expansion',
      'sitecustomize injection for Slurm compute nodes',
    ],
    dashboardUi: 'Capture tab (Layer C deferred) — captures appear in Runs tab after running from CLI. Docker runner options (image, network, workdir, user) are configurable in Commands tab → nova capture.',
    cliCommands: ['nova capture', 'nova capture --runner docker', 'nova mcp-proxy', 'nova api-proxy', 'nova new-run-id'],
  },
  {
    title: 'Parent / Child Capsules',
    phase: 'Phase 3 — v0.12',
    badge: 'shipped',
    description: 'Distributed-run capsule hierarchy — one PARENT capsule per job, one WORKER capsule per rank/process. Typed edge vocabulary (ADR-0044). Fail-open out-of-order delivery (ADR-0045). Two-phase lifecycle (ADR-0046).',
    shipped: [
      'nova run show --with-children — view full hierarchy',
      'nova run validate-distributed — schema + lifecycle validation',
      'nova run lineage --edge-types — typed edge lineage query',
      'PARTIALLY_COMPLETE state for straggler workers',
      '123 tests; blast-radius FP rate 0% (vs 25% untyped)',
    ],
    dashboardUi: null,
    cliCommands: ['nova run show', 'nova run validate-distributed'],
    note: 'Dashboard UI for hierarchy view is planned for a future release. Use the Runs tab to inspect individual capsules.',
  },
  {
    title: 'NovaSeal Cryptographic Core',
    phase: 'Phase 0 — v0.12',
    badge: 'shipped',
    description: 'End-to-end capsule signing — DSSE envelope (ECDSA P-256), RFC 3161 timestamp adapter, SQLite Merkle log, config auto-discovery (ADR-0041).',
    shipped: [
      'nova verify — verify DSSE signature + RFC 3161 timestamp + Merkle log inclusion',
      'Opt-in orchestrator hook (_seal_capsule after capsule.yaml write)',
      '97 tests in tests/seal/',
    ],
    dashboardUi: 'Evidence tab — verify button on bundles. nova verify is also in the Commands tab.',
    cliCommands: ['nova verify', 'nova export-evidence'],
  },
  {
    title: 'Metadata DB (Production)',
    phase: 'Phase 5 — v0.12',
    badge: 'partial',
    description: 'Production MetadataStore with Postgres RLS, row-level tenant isolation, pgBouncer compatibility, and dual-track Alembic migrations (ADR-0050, 0051, 0052).',
    shipped: [
      'MetadataStore ABC + SQLiteMetadataStore (dev) + PostgresMetadataStore (SET LOCAL RLS)',
      'nova db migrate-to-postgres — one-time SQLite → Postgres migration',
      'nova db upgrade — run Alembic schema upgrades',
      'Cross-tenant isolation test (GDPR Article 32 control); zero row leaks',
      'query_runs p99 benchmark harness (DEFERRED FR-17 at 16-vCPU gate)',
    ],
    dashboardUi: 'Server mode (nova serve --postgres-url) uses PostgresMetadataStore automatically. No separate admin UI yet.',
    cliCommands: ['nova db migrate-to-postgres'],
    note: 'ADR-0050/0051/0052 are Proposed — promoted to Accepted after pgBouncer+RLS citation resolved and 10M-row benchmark passes.',
  },
  {
    title: 'Lineage at Scale (KuzuDB / Federation)',
    phase: 'Phase 6 — v0.12',
    badge: 'partial',
    description: 'Multi-backend lineage store (SQLite → Postgres → KuzuDB → AGE/JanusGraph) with shard federation, sovereignty tokens, and OpenLineage emission (ADR-0053).',
    shipped: [
      'AbstractLineageStore ABC + SQLiteLineageStore + KuzuLineageStore + PostgresLineageStore stub',
      'nova lineage-store migrate — migration between backends',
      'nova-lineage-bench harness (7 SQL+Cypher templates, DuckDB/Postgres/KuzuDB runners)',
      'Federation layer: shard_query, coordinator, summary, sovereignty tokens',
      'nova lineage emit-openlineage — OLAF-compatible event emission',
      '132 lineage tests',
    ],
    dashboardUi: 'Lineage tab — full edge browser + provenance / blast-radius / replay-chain query panel. Time-travel query (ADR-0047) is CLI-only for now.',
    cliCommands: ['nova lineage provenance', 'nova lineage blast-radius', 'nova lineage replay-chain', 'nova lineage time-travel', 'nova lineage emit-openlineage'],
    note: 'B-7 scale benchmarks (10M/100M edges) are hardware-gated. KuzuDB production promotion pending benchmark. OQ-04 (federation protocol) is open.',
  },
  {
    title: 'Server Mode (Postgres, OIDC, RBAC)',
    phase: 'v0.7',
    badge: 'partial',
    description: 'Multi-user server mode — Postgres backend, REST API, OIDC Device Auth, RBAC with offline tokens (ADR-0018). Server admin operations are CLI-only.',
    shipped: [
      'nova serve --experimental — starts dashboard (this UI)',
      'nova login / nova logout — OIDC device auth',
      'nova server start — server mode daemon',
      'nova server issue-token / revoke-token / assign-role / flush-jwks-cache',
      'REST API + token verification',
    ],
    dashboardUi: 'Dashboard runs on top of nova serve. Admin operations (token issuance, role assignment) are CLI-only.',
    cliCommands: ['nova serve', 'nova login', 'nova server start'],
    note: 'Dashboard Layer B (mutations) is fully operational. Layer C (launching captures from browser) is deferred pending security review.',
  },
  {
    title: 'Policy + Approval Gates',
    phase: 'v0.8',
    badge: 'shipped',
    description: 'OPA/Rego policy engine for eval-gated promotion and maker-checker approvals (ADR-0031). WORM storage conformance. Audit log.',
    shipped: [
      'nova policy test — interactive policy tester',
      'nova policy explain — decision trace',
      'nova approve — record sign-off',
      'nova promote — eval-gated lifecycle transitions',
      'nova hold create / release / list — legal holds',
      'regression_gate.rego — Rego-gated promotion on eval regression',
    ],
    dashboardUi: 'Policy tab (interactive tester), Holds tab (create/release), Registry tab (promote + eval).',
    cliCommands: ['nova policy test', 'nova policy explain', 'nova approve', 'nova promote', 'nova rollback'],
  },
  {
    title: 'Standard Eval Suites',
    phase: 'v0.9',
    badge: 'shipped',
    description: 'OCI-pinned standard benchmark adapters — GAIA L1, SWE-bench Lite, AgentBench OS, MMLU v1, TruthfulQA v1, Smoke v1. Rego-gated promotion on regression (ADR-0033).',
    shipped: [
      'nova eval run — run any eval suite against a registered asset',
      'EvalSuiteAdapter protocol + plugin entry-point loader',
      'novafabric-smoke-v1 (host-env, no container)',
      'gaia-l1, swe-bench-lite, agentbench-os (OCI-pinned)',
      'mmlu-v1, truthfulqa-v1 adapters',
      'Statistical significance testing for regression detection',
    ],
    dashboardUi: 'Registry tab — run eval + Eval Trend sparkline + history chart. See also: Commands tab → nova eval run.',
    cliCommands: ['nova eval run'],
  },
];

function CmdBadge({ cmd }: { cmd: string }) {
  return (
    <code className="inline-block font-mono text-[10px] px-1.5 py-0.5 rounded bg-[var(--color-bg-sunken)] border border-[var(--color-border)] text-[var(--color-text-muted)]">
      {cmd}
    </code>
  );
}

function Card({ c }: { c: ComponentCard }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">{c.title}</h3>
            <span className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR[c.badge]}`}>
              {BADGE_LABEL[c.badge]}
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">{c.phase}</div>
        </div>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">{c.description}</p>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">Shipped</div>
        <ul className="space-y-0.5">
          {c.shipped.map((s, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[var(--color-text-muted)]">
              <span className="shrink-0 mt-px text-[var(--color-status-success)]">✓</span>
              {s}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-[11px] leading-relaxed">
        <span className="font-medium text-[var(--color-text)]">Dashboard: </span>
        {c.dashboardUi ? (
          <span className="text-[var(--color-text-muted)]">{c.dashboardUi}</span>
        ) : (
          <span className="text-[var(--color-status-pending)]">No dashboard UI yet — CLI only.</span>
        )}
      </div>

      {c.cliCommands.length > 0 && (
        <div className="flex flex-wrap gap-1.5 items-center">
          <span className="text-[10px] text-[var(--color-text-faint)]">CLI:</span>
          {c.cliCommands.map((cmd) => <CmdBadge key={cmd} cmd={cmd} />)}
        </div>
      )}

      {c.note && (
        <p className="text-[11px] text-[var(--color-text-faint)] italic leading-relaxed border-l-2 border-[var(--color-border)] pl-2">
          {c.note}
        </p>
      )}
    </div>
  );
}

// ---------- shared helpers ----------

function StatRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[10px] text-[var(--color-text-faint)] shrink-0">{label}:</span>
      <span className="text-[11px] text-[var(--color-text-muted)] truncate">{value}</span>
    </div>
  );
}

// ---------- DD-2: Collector Status Card ----------

type CollectorData = Awaited<ReturnType<typeof api.collectorStatus>>;

function CollectorCard() {
  const [data, setData] = useState<CollectorData | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);

  const doFetch = () => {
    setLoading(true);
    api.collectorStatus()
      .then((d) => {
        setData(d);
        setLastChecked(new Date());
      })
      .catch(() => {/* silently ignore — endpoint may not be available yet */})
      .finally(() => setLoading(false));
  };

  // Initial load on mount; recurring poll pauses while the tab is hidden.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { doFetch(); }, []);
  usePolling(doFetch, 5000);

  const relativeTime = lastChecked
    ? (() => {
        const sec = Math.floor((Date.now() - lastChecked.getTime()) / 1000);
        if (sec < 5) return 'just now';
        if (sec < 60) return `${sec}s ago`;
        return `${Math.floor(sec / 60)}m ago`;
      })()
    : null;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">Collector — Cluster-Scale Event Ingestion</h3>
            <span className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
              live
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">Phase 2 — v0.12</div>
        </div>
        <button
          onClick={doFetch}
          disabled={loading}
          className="shrink-0 text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50"
        >
          {loading ? 'Checking…' : 'Refresh'}
        </button>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Go-based high-throughput event collector for HPC/Kubernetes environments — NATS leaf node lifecycle, cap-001 rename-commit JSONL spool, NovaSeal batch signing, Prometheus metrics (ADR-0043).
      </p>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">Shipped</div>
        <ul className="space-y-0.5">
          {[
            'novafabric-collector binary (Go, github.com/novafabric/collector)',
            'novafabric-verifier — offline signature verifier',
            'novafabric-hpc-hub — NATS leaf lifecycle wrapper',
            'deploy/hpc/ — prolog.sh, epilog.sh, NATS templates, Ansible playbook',
            'deploy/k8s/ — DaemonSet (Fluent Bit), ConfigMap, Secret',
            '1000-event deterministic reference corpus validated',
            'Target: 100K events/sec at spool; NovaSeal p99 < 200ms',
          ].map((s, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[var(--color-text-muted)]">
              <span className="shrink-0 mt-px text-[var(--color-status-success)]">✓</span>
              {s}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2.5 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)]">Live Status</span>
          {relativeTime && (
            <span className="text-[10px] text-[var(--color-text-faint)]">Last checked: {relativeTime}</span>
          )}
        </div>

        {data === null ? (
          <p className="text-[11px] text-[var(--color-text-faint)]">Checking collector…</p>
        ) : data.detected ? (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
            <StatRow label="Status" value={<span className="text-[var(--color-status-success)] font-medium">Detected</span>} />
            <StatRow label="Version" value={data.collector_version ?? 'unknown'} />
            <StatRow label="Spool lag" value={data.spool_lag != null ? String(data.spool_lag) : 'N/A'} />
            <StatRow label="Signing p99" value={data.signing_p99_ms != null ? `${data.signing_p99_ms.toFixed(1)} ms` : 'N/A'} />
            <StatRow label="Events/sec" value={data.events_per_sec != null ? data.events_per_sec.toLocaleString() : 'N/A'} />
            <StatRow label="Heartbeat" value={data.last_heartbeat ?? 'N/A'} />
            {data.source && (
              <div className="col-span-2">
                <StatRow label="Source" value={<code className="font-mono text-[10px]">{data.source}</code>} />
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-[var(--color-status-pending)]" />
              <span className="text-[11px] text-[var(--color-status-pending)] font-medium">Collector not detected</span>
            </div>
            <p className="text-[11px] text-[var(--color-text-faint)] leading-relaxed">
              Install the collector binary or deploy to your cluster:
            </p>
            <ul className="space-y-1 text-[11px] text-[var(--color-text-muted)]">
              <li>
                <code className="font-mono text-[10px] bg-[var(--color-bg-raised)] px-1 py-0.5 rounded border border-[var(--color-border)]">
                  go install github.com/novafabric/collector/cmd/novafabric-collector@latest
                </code>
              </li>
              <li>Or use cluster configs: <code className="font-mono text-[10px]">deploy/hpc/</code> or <code className="font-mono text-[10px]">deploy/k8s/</code></li>
              <li>
                Health file: <code className="font-mono text-[10px]">~/.novafabric/collector-health.json</code>{' '}
                or <code className="font-mono text-[10px]">/tmp/novafabric-collector-health.json</code>
              </li>
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- Docker Runner Card ----------

function DockerRunnerCard() {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">Docker Runner</h3>
            <span className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
              {BADGE_LABEL['partial']}
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">v0.6 — ADR-0025</div>
        </div>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Runs <code className="font-mono">nova capture</code> workloads inside a <code className="font-mono">docker run</code> container. Mounts the capsule directory as a volume; forwards stdout/stderr. Requires the image to have novafabric pip-installed.
      </p>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">Shipped</div>
        <ul className="space-y-0.5">
          {[
            'image — required: fully-qualified image reference',
            'network — docker network (default: bridge)',
            'workdir — container working directory',
            'user — run as uid:gid (e.g. 1000:1000)',
            'extra_volumes — list of host:container[:opts] mounts',
            'extra_env — dict of env vars injected alongside NOVAFABRIC_*',
            'Eval containers: OCI digest-pinned via run_eval_container (ADR-0033)',
            'Anti-patterns enforced: no --privileged, no host namespaces, no socket mount, no hardcoded registry',
          ].map((s, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[var(--color-text-muted)]">
              <span className="shrink-0 mt-px text-[var(--color-status-success)]">✓</span>
              {s}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 text-[11px] leading-relaxed">
        <span className="font-medium text-[var(--color-text)]">Dashboard: </span>
        <span className="text-[var(--color-text-muted)]">
          Docker runner options are surfaced in the Commands tab &rarr; nova capture &rarr; runner=docker.
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-[10px] text-[var(--color-text-faint)]">CLI:</span>
        <CmdBadge cmd="nova capture --runner docker" />
        <CmdBadge cmd="nova eval run" />
      </div>

      <p className="text-[11px] text-[var(--color-text-faint)] italic leading-relaxed border-l-2 border-[var(--color-border)] pl-2">
        Configure defaults in <code className="font-mono">.novafabric/runners.yaml</code> to avoid repeating <code className="font-mono">--runner-option</code> on every invocation.
      </p>
    </div>
  );
}

// ---------- DD-7: Object Store Browser Card ----------

type StorageStats = Awaited<ReturnType<typeof api.storageStats>>;
type ManifestChain = Awaited<ReturnType<typeof api.storageManifestChain>>;

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function ObjectStoreCard() {
  const [stats, setStats] = useState<StorageStats | null>(null);
  const [chain, setChain] = useState<ManifestChain | null>(null);
  const [loading, setLoading] = useState(false);

  const doFetch = () => {
    setLoading(true);
    Promise.all([
      api.storageStats(),
      api.storageManifestChain(20),
    ])
      .then(([s, c]) => {
        setStats(s);
        setChain(c);
      })
      .catch(() => {/* silently ignore */})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    doFetch();
  }, []);

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">Object Capsule Store</h3>
            <span className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR['partial']}`}>
              live
            </span>
          </div>
          <div className="text-[10px] text-[var(--color-text-faint)] mt-0.5 font-mono">Phase 4 — v0.12</div>
        </div>
        <button
          onClick={doFetch}
          disabled={loading}
          className="shrink-0 text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Content-addressable capsule storage with manifest chain (Iceberg-style), checkpoint compaction, NovaSeal write contract, and WORM backend adapters (S3/MinIO/Ceph/Azure/GCS) — ADR-0047, 0048, 0049.
      </p>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">Shipped</div>
        <ul className="space-y-0.5">
          {[
            'put_capsule p99 ≤ 350ms; 100K rebuild in 0.77s',
            'WORM conformance suite (10/10): nova-worm-test CLI + signed JSON attestation',
            'Local WAL + S3/MinIO/Ceph/Azure adapters; GCS stub',
            'Manifest chain rebuild test passes',
          ].map((s, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[var(--color-text-muted)]">
              <span className="shrink-0 mt-px text-[var(--color-status-success)]">✓</span>
              {s}
            </li>
          ))}
        </ul>
      </div>

      {/* Live storage stats */}
      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2.5 space-y-2">
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)]">Storage Stats</div>

        {stats === null ? (
          <p className="text-[11px] text-[var(--color-text-faint)]">Loading…</p>
        ) : !stats.configured ? (
          <div className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-[var(--color-status-pending)]" />
              <span className="text-[11px] text-[var(--color-status-pending)] font-medium">Object store not configured</span>
            </div>
            <p className="text-[11px] text-[var(--color-text-faint)]">
              Set one of these environment variables to enable:
            </p>
            <ul className="space-y-0.5 text-[11px] text-[var(--color-text-muted)]">
              <li><code className="font-mono text-[10px]">NOVA_OBJECT_STORE_PATH</code> — local WAL path</li>
              <li><code className="font-mono text-[10px]">NOVA_S3_BUCKET</code> — S3/MinIO/Ceph bucket</li>
            </ul>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
            <StatRow label="Backend" value={stats.backend_type} />
            <StatRow label="Chunks" value={stats.total_chunks != null ? stats.total_chunks.toLocaleString() : 'N/A'} />
            <StatRow label="Total size" value={stats.total_size_bytes != null ? formatBytes(stats.total_size_bytes) : 'N/A'} />
            <StatRow label="WORM score" value={stats.worm_score != null ? String(stats.worm_score) : 'N/A'} />
            <StatRow label="Chain head" value={stats.manifest_chain_head ?? 'N/A'} />
            <StatRow label="put_capsule p99" value={stats.last_put_p99_ms != null ? `${stats.last_put_p99_ms} ms` : 'N/A'} />
            {stats.error && (
              <div className="col-span-2 text-[10px] text-[var(--color-status-pending)]">{stats.error}</div>
            )}
          </div>
        )}
      </div>

      {/* Manifest chain browser */}
      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2.5 space-y-2">
        <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)]">Manifest Chain (last 20)</div>

        {chain === null ? (
          <p className="text-[11px] text-[var(--color-text-faint)]">Loading…</p>
        ) : chain.entries.length === 0 ? (
          <p className="text-[11px] text-[var(--color-text-faint)] italic">No manifest entries yet.</p>
        ) : (
          <div style={{ maxHeight: 200, overflowY: 'auto' }}>
            <table className="w-full text-[10px]">
              <thead>
                <tr className="text-left text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                  <th className="pb-1 pr-3 font-medium">Hash</th>
                  <th className="pb-1 pr-3 font-medium">Run ID</th>
                  <th className="pb-1 pr-3 font-medium">Timestamp</th>
                  <th className="pb-1 font-medium">Size</th>
                </tr>
              </thead>
              <tbody>
                {chain.entries.map((e, i) => (
                  <tr key={i} className="border-b border-[var(--color-border)] last:border-0">
                    <td className="py-1 pr-3 font-mono text-[var(--color-text-muted)]">{e.hash}</td>
                    <td className="py-1 pr-3 font-mono text-[var(--color-text-muted)] max-w-[120px] truncate">{e.run_id || '—'}</td>
                    <td className="py-1 pr-3 text-[var(--color-text-faint)]">{e.timestamp || '—'}</td>
                    <td className="py-1 text-[var(--color-text-faint)]">{formatBytes(e.size_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="text-[11px] text-[var(--color-text-faint)] italic leading-relaxed border-l-2 border-[var(--color-border)] pl-2">
        Object store is configured via environment variables and used transparently by nova capture.
      </p>
    </div>
  );
}

// ---------- DB-STG-1: Storage operations card (v0.18.0, cap-003/009) ----------

function StorageOpsCard({ runIds }: { runIds: string[] }) {
  const [endpoint, setEndpoint] = useState('');
  const [bucket, setBucket] = useState('nova-capsules');
  const [valLoading, setValLoading] = useState(false);
  const [valResult, setValResult] = useState<{ ok: boolean; result?: Record<string, unknown>; error?: string; error_class?: string } | null>(null);

  const [runId, setRunId] = useState('');
  const [inspLoading, setInspLoading] = useState(false);
  const [inspResult, setInspResult] = useState<{ audit_object_key: string; pii_object_key: string | null; cap003_enabled: boolean; note: string } | null>(null);
  const [inspErr, setInspErr] = useState<string | null>(null);

  const validate = useCallback(async () => {
    setValLoading(true);
    setValResult(null);
    try {
      const r = await api.storageValidate(endpoint || undefined, bucket || 'nova-capsules');
      setValResult(r);
    } catch (e) {
      setValResult({ ok: false, error: (e as Error).message });
    } finally {
      setValLoading(false);
    }
  }, [endpoint, bucket]);

  const inspect = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setInspLoading(true);
    setInspErr(null);
    setInspResult(null);
    try {
      const r = await api.storageInspect(id);
      setInspResult(r);
    } catch (e) {
      setInspErr((e as Error).message);
    } finally {
      setInspLoading(false);
    }
  }, [runId]);

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Storage Operations (cap-003 / cap-009)</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            S3 Object-Lock COMPLIANCE validation · dual-object split inspection
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
          ADR-0066
        </span>
      </div>

      {/* Validate panel */}
      <div className="space-y-2 border-b border-[var(--color-border)] pb-3">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Validate S3 Object Lock</p>
        <div className="grid grid-cols-2 gap-2">
          <input
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="https://s3.amazonaws.com (optional)"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
          <input
            value={bucket}
            onChange={(e) => setBucket(e.target.value)}
            placeholder="nova-capsules"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
        </div>
        <button
          onClick={validate}
          disabled={valLoading}
          className="text-xs font-mono px-3 py-1 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white transition-colors disabled:opacity-50"
        >
          {valLoading ? 'validating…' : 'Validate'}
        </button>
        {valResult && (
          <div className={clsx(
            'text-[11px] font-mono rounded border px-3 py-2',
            valResult.ok
              ? 'border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]'
          )}>
            {valResult.ok ? 'OK — ' + JSON.stringify(valResult.result) : (valResult.error_class || 'error') + ': ' + valResult.error}
          </div>
        )}
      </div>

      {/* Inspect panel */}
      <div className="space-y-2">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Inspect dual-object split for run</p>
        <div className="flex gap-2">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={inspect}
            placeholder="run_2026_..."
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono flex-1"
          />
          <button
            onClick={inspect}
            disabled={inspLoading || !runId.trim()}
            className="text-xs font-mono px-3 py-1 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white transition-colors disabled:opacity-50"
          >
            {inspLoading ? 'inspecting…' : 'Inspect'}
          </button>
        </div>
        {inspErr && (
          <div className="text-[11px] font-mono text-[var(--color-status-failure)]">{inspErr}</div>
        )}
        {inspResult && (
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 space-y-1 text-[11px] font-mono">
            <div><span className="text-[var(--color-text-faint)]">audit:</span> {inspResult.audit_object_key}</div>
            <div>
              <span className="text-[var(--color-text-faint)]">pii:</span>{' '}
              {inspResult.pii_object_key ?? <span className="text-[var(--color-text-faint)]">(none — NOVA_CAP003_ENABLED=false)</span>}
            </div>
            <div className="text-[10px] text-[var(--color-text-muted)] pt-1 border-t border-[var(--color-border)] mt-1">{inspResult.note}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- Lineage Store Deployment Profile panel (nova lineage-store profile — v0.46.0) ----------

function LineageStoreProfilePanel() {
  const [target, setTarget] = useState('kuzudb-vertical');
  const [nodeSize, setNodeSize] = useState('16g-ram-500g-nvme');
  const [rf, setRf] = useState(3);
  const [imageTag, setImageTag] = useState('latest');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.lineageStoreProfile>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.lineageStoreProfile({
        target,
        node_size: target === 'kuzudb-vertical' ? nodeSize : undefined,
        rf: target === 'janusgraph-minimal' ? rf : undefined,
        image_tag: imageTag || undefined,
      });
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [target, nodeSize, rf, imageTag]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Lineage Store Deployment Profile</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Generate a docker-compose profile for a lineage backend — <code className="font-mono">nova lineage-store profile</code>
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">v0.46</span>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <select
          value={target}
          onChange={e => setTarget(e.target.value)}
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
        >
          <option value="kuzudb-vertical">kuzudb-vertical</option>
          <option value="janusgraph-minimal">janusgraph-minimal</option>
        </select>
        {target === 'kuzudb-vertical' ? (
          <input
            value={nodeSize}
            onChange={(e) => setNodeSize(e.target.value)}
            placeholder="16g-ram-500g-nvme"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
        ) : (
          <input
            type="number"
            min={1}
            value={rf}
            onChange={(e) => setRf(Math.max(1, Number(e.target.value) || 1))}
            placeholder="3"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
        )}
        <input
          value={imageTag}
          onChange={(e) => setImageTag(e.target.value)}
          placeholder="latest"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
        />
      </div>
      <button
        onClick={generate}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Generate'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">target: {result.target}</span>
            <CopyButton text={result.profile_yaml} label="yaml" />
          </div>
          <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2.5 whitespace-pre-wrap text-[var(--color-text-muted)] max-h-64 overflow-auto">
            {result.profile_yaml}
          </pre>
        </div>
      )}
    </section>
  );
}

// ---------- Main tab ----------

export default function InfraTab() {
  const [runIds, setRunIds] = useState<string[]>([]);
  useEffect(() => {
    api.listRuns().then(r => setRunIds(r.runs.map(run => run.run_id))).catch(() => {});
  }, []);
  const shipped = COMPONENTS.filter(c => c.badge === 'shipped').length;
  const partial = COMPONENTS.filter(c => c.badge === 'partial').length + 3; // +3 for live cards (Collector, DockerRunner, ObjectStore)
  const cliOnly = 0;

  return (
    <div className="space-y-4 max-w-4xl">
      {/* Header */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-5 py-4">
        <h2 className="text-sm font-semibold text-[var(--color-text)] mb-1">Infrastructure & Cluster-Scale Components</h2>
        <p className="text-xs text-[var(--color-text-muted)] leading-relaxed mb-3">
          All cluster-scale phases (Phases 0–6) shipped in v0.12. This tab shows the status of each component, what is available in the dashboard, and what requires the CLI.
        </p>
        <div className="flex flex-wrap gap-3">
          <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
            <span className="w-2 h-2 rounded-full bg-[var(--color-status-success)]" />
            {shipped} full dashboard UI
          </span>
          <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
            <span className="w-2 h-2 rounded-full bg-[var(--color-accent)]" />
            {partial} partial UI
          </span>
          <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
            <span className="w-2 h-2 rounded-full bg-[var(--color-status-pending)]" />
            {cliOnly} CLI only
          </span>
        </div>
      </div>

      {/* Live cards first, then static cards */}
      <div className="grid grid-cols-1 gap-3">
        <CollectorCard />
        <DockerRunnerCard />
        <ObjectStoreCard />
        <StorageOpsCard runIds={runIds} />
        <LineageStoreProfilePanel />
        {COMPONENTS.map((c) => <Card key={c.title} c={c} />)}
      </div>

      <MCPScanPanel />

      <MCPRiskReportPanel />

      {/* Footer note */}
      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-4 py-3 text-[11px] text-[var(--color-text-faint)] leading-relaxed">
        <strong className="text-[var(--color-text)]">CLI-only operations</strong> are covered in the{' '}
        <span className="font-mono">Commands</span> tab — use the command builder to construct and copy any CLI command to your terminal.
        The dashboard auto-refreshes every 5 seconds when auto-refresh is enabled.
      </div>
    </div>
  );
}

// ---------- MCP Risk Report panel (nova mcp risk-report — structured OWASP report) ----------

function MCPRiskReportPanel() {
  const [manifestJson, setManifestJson] = useState('{\n  "name": "my-server",\n  "version": "1.0.0",\n  "tools": []\n}');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.mcpRiskReport>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const manifest = JSON.parse(manifestJson) as Record<string, unknown>;
      const r = await api.mcpRiskReport(manifest);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [manifestJson]);

  const riskCls = (level: string) => {
    switch (level) {
      case 'CRITICAL':
      case 'HIGH':
        return 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]';
      case 'MEDIUM':
        return 'text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_10%,transparent)]';
      default:
        return 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]';
    }
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">MCP Risk Report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Generate a structured OWASP LLM risk report for an MCP manifest — <code className="font-mono">nova mcp risk-report</code>
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">E-9</span>
      </div>
      <textarea
        value={manifestJson}
        onChange={e => setManifestJson(e.target.value)}
        rows={6}
        className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none resize-y"
      />
      <button
        onClick={generate}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Generate Report'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && !result.ok && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {result.note ?? 'Unknown error'}
        </div>
      )}
      {result && result.ok && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={clsx('text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded', riskCls(result.overall_risk_level ?? 'LOW'))}>
              {result.overall_risk_level ?? 'LOW'}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.total_findings ?? 0} finding{(result.total_findings ?? 0) !== 1 ? 's' : ''}
            </span>
            {result.server_name && (
              <span className="text-[10px] font-mono text-[var(--color-text-faint)]">· {result.server_name}</span>
            )}
          </div>
          {(result.total_findings ?? 0) === 0 ? (
            <p className="text-[10px] text-[var(--color-status-success)]">✓ No risks detected</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--color-border)] text-[var(--color-text-faint)]">
                    <th className="text-left py-1 pr-3">Tool</th>
                    <th className="text-right py-1 pr-3">Risk Score</th>
                    <th className="text-left py-1 pr-3">Highest Severity</th>
                    <th className="text-right py-1">Findings</th>
                  </tr>
                </thead>
                <tbody>
                  {(result.tools ?? []).filter(t => t.findings.length > 0).map(tool => (
                    <tr key={tool.tool_name} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-bg-sunken)]">
                      <td className="py-1 pr-3 text-[var(--color-text)] truncate max-w-[160px]">{tool.tool_name}</td>
                      <td className="py-1 pr-3 text-right text-[var(--color-text-faint)]">{tool.risk_score.toFixed(1)}</td>
                      <td className="py-1 pr-3">
                        <span className={clsx('px-1 py-0.5 rounded text-[9px]', riskCls(tool.highest_severity))}>
                          {tool.highest_severity}
                        </span>
                      </td>
                      <td className="py-1 text-right text-[var(--color-text-faint)]">{tool.findings.length}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- MCP Supply-Chain Risk Scanner panel (nova mcp scan — E-9) ----------

function MCPScanPanel() {
  const [manifestJson, setManifestJson] = useState('{\n  "name": "my-server",\n  "version": "1.0.0",\n  "tools": []\n}');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.mcpScan>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const scan = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const manifest = JSON.parse(manifestJson) as Record<string, unknown>;
      const r = await api.mcpScan(manifest);
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [manifestJson]);

  const riskCls = (level: string) => {
    switch (level) {
      case 'CRITICAL':
      case 'HIGH':
        return 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]';
      case 'MEDIUM':
        return 'text-[var(--color-status-warning)] bg-[color-mix(in_oklab,var(--color-status-warning)_10%,transparent)]';
      default:
        return 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]';
    }
  };

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">MCP Supply-Chain Risk Scanner</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Scan an MCP server manifest for OWASP LLM supply-chain risks — <code className="font-mono">nova mcp scan</code>
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">E-9</span>
      </div>
      <textarea
        value={manifestJson}
        onChange={e => setManifestJson(e.target.value)}
        rows={6}
        className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none resize-y"
      />
      <button
        onClick={scan}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Scan Manifest'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className={clsx('text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded', riskCls(result.overall_risk_level))}>
              {result.overall_risk_level}
            </span>
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">
              {result.total_findings} finding{result.total_findings !== 1 ? 's' : ''} in {result.tools.length} tool{result.tools.length !== 1 ? 's' : ''}
            </span>
          </div>
          {result.total_findings === 0 ? (
            <p className="text-[10px] text-[var(--color-status-success)]">✓ No risks detected</p>
          ) : (
            <div className="space-y-1.5 max-h-52 overflow-y-auto">
              {result.tools.filter(t => t.findings.length > 0).map(tool => (
                <div key={tool.tool_name} className="space-y-0.5">
                  <p className="text-[10px] font-mono font-semibold text-[var(--color-text)]">{tool.tool_name}</p>
                  {tool.findings.map((f, i) => (
                    <div key={i} className="flex items-start gap-1.5 text-[10px] ml-2">
                      <span className={clsx('font-mono shrink-0', riskCls(f.severity).split(' ')[0])}>
                        [{f.severity}]
                      </span>
                      <span className="text-[var(--color-text)] leading-tight">{f.message}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
