// Static component-status cards for the Infra tab. Extracted verbatim from
// InfraTab.tsx (dashboard-modernization split).
import { BADGE_COLOR, BADGE_LABEL, CmdBadge, type StatusBadge } from './badges';

export interface ComponentCard {
  title: string;
  phase: string;
  badge: StatusBadge;
  description: string;
  shipped: string[];
  dashboardUi: string | null;
  cliCommands: string[];
  note?: string;
}

export const COMPONENTS: ComponentCard[] = [
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

export function Card({ c }: { c: ComponentCard }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[var(--color-text)]">{c.title}</h3>
            <span className={`text-[var(--text-2xs)] uppercase tracking-wider px-1.5 py-0.5 rounded border font-medium ${BADGE_COLOR[c.badge]}`}>
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
