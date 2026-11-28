# Architecture

> Verified against code on 2026-11-27.

A contributor's map of the codebase: what the subsystems are, where they live,
and which invariants you must not break. For the *conceptual* model — what a Run
Capsule is, how replay modes differ — read [concepts.md](concepts.md) first.

> **Illustrated, as-built deep dives** — animated diagrams, code-path citations
> and maturity labels for every subsystem — live in
> [`docs/architecture/`](architecture/README.md): [overview & the five
> primitives](architecture/overview.md) · [the pipeline](architecture/pipeline.md)
> · [Run Capsule anatomy](architecture/run-capsule.md) · [sealing &
> verification](architecture/sealing-and-verification.md) · [replay
> modes](architecture/replay-modes.md) · [lineage graph](architecture/lineage-graph.md)
> · [deployment topologies](architecture/deployment-topologies.md) · and an
> offline [interactive explainer](architecture/explainer.html).

> **New here?** The fastest orientation is: read
> [Concepts](concepts.md) → skim [the subsystem map](#subsystem-map) below →
> pick a [good first issue](https://github.com/MSKazemi/novafabric/labels/good%20first%20issue).
> You do not need to understand the whole system to fix a bug in one subsystem.

---

## Framing

> **Local-first now. Distributed-ready always. Cluster-scale later.**

A one-node run is the smallest case of a distributed run. The architecture
deliberately avoids assumptions that would block the cluster-scale direction:
that one run equals one process, that one capsule has one writer, that every
agent is Python, that all capture is full capture, or that events arrive in
order.

SQLite is the local-mode default; Postgres is the server-mode backend.
**Postgres is never required for local mode** — if you find a code path that
makes it required, that is a bug worth reporting.

---

## System context

```
        your workload                     NovaFabric                     artifacts
  ┌────────────────────────┐      ┌───────────────────────┐      ┌────────────────────┐
  │ script / notebook cell │      │  capture (hooks +     │      │  Run Capsule       │
  │ agent / LLM app        │─────▶│  proxies + runners)   │─────▶│  ~/.novafabric/    │
  │ HPC training job       │      │                       │      │    capsules/<ulid>/│
  └────────────────────────┘      └───────────┬───────────┘      └─────────┬──────────┘
                                              │                            │
                                   ┌──────────▼──────────┐      ┌──────────▼──────────┐
                                   │ seal · redact ·     │      │ replay · diff ·     │
                                   │ validate            │      │ lineage · evidence  │
                                   └─────────────────────┘      └─────────────────────┘
```

The unit of value is **a portable folder you own**, not a row in a hosted
database. Every design decision follows from that: no server is required, no
account is required, nothing phones home, and a capsule read air-gapped five
years from now must still verify.

---

## Subsystem map

Everything lives under `src/novafabric/`. Sizes are a rough guide to where the
weight is, not a quality signal.

### The core path — capture to capsule

| Module | What it does |
|---|---|
| `capture/` | The capture orchestrator, hook installation, and the event recorder. **The hot path** — changes here need benchmarks. |
| `runners/` | Where the captured workload actually executes: local process (the default), Docker container, SLURM, Kubernetes, LSF and PBS (`runners/_registry.py`). |
| `proxy/` | Transparent HTTP proxies for providers that cannot be hooked in-process. |
| `adapters/` | Framework/integration adapters including LangGraph, AutoGen, CrewAI, DSPy, OpenAI Agents, Google ADK, Bedrock AgentCore, A2A, LlamaIndex, Pydantic AI, Haystack, smolagents, the LangChain tool seam (`langchain_tools.py`), Git, Langfuse, and MLflow. MCP capture lives under the capture/proxy paths (`capture/hooks/_mcp.py`, `proxy/mcp_proxy.py`) rather than this adapter directory. |
| `capsule/` | Capsule construction, validation, parent/child relationships. |
| `spec/`, `registry/` | Asset Spec models and the local asset registry. |
| `masking/`, `pii/` | Redaction pipeline and pluggable maskers. |

### Verification and trust

| Module | What it does |
|---|---|
| `trust/` | NovaSeal — signing, Merkle trees, timestamping, transparency witnesses. |
| `evidence/`, `evidence_fabric/` | Evidence Bundle construction and export. |
| `provenance/`, `supplychain/`, `envelopes/` | Attestations and AI-BOM; `envelopes/` holds the DSSE, in-toto and SLSA attestation envelopes (not to be confused with `envelope/`, below). |
| `audit/`, `governance/`, `policies/`, `policy/` | Policy gates and the audit trail. |

### Reasoning over runs

| Module | What it does |
|---|---|
| `replay/` | The replay engine and its modes. **A shared surface — coordinate before large changes.** |
| `diff/` | Structural diff between two capsules. |
| `lineage/` | Lineage graph and its storage backends (SQLite, Kuzu, Postgres, Apache AGE, JanusGraph). |
| `kg/` | The security & provenance knowledge graph. |
| `diagnose/` | Causal-graph attribution and counterfactual root-cause search. |
| `query/` | The offline capsule query DSL. |
| `eval/`, `evals/`, `judge/`, `scores.py` | Evaluation harness, scoring, regression gates. |
| `assure/` | Assurance cases, golden baselines, canary records, the impact report and the standing regression alarm (ADR-0147, ADR-0166). |
| `drift/` | Offline drift detection over sealed runs: two-sample drift, silent-failure flags, root-cause correlation and behavioral fingerprints (ADR-0147). |
| `a2a/` | A2A interoperability evidence: message envelopes, the portable Agent Card, and Task/Message/Artifact object mapping (ADR-0142, ADR-0149). |

### Serving and scale

| Module | What it does |
|---|---|
| `cli/` | Every `nova` subcommand. The largest module by file count — and the most approachable. `cli/introspect.py` is the one supported way to enumerate the command tree (ADR-0250). |
| `server/` | Server mode: REST API, auth, API keys, tenancy. |
| `serve/` | The dashboard backend that `nova serve` exposes. `serve/introspect.py` is the one supported way to enumerate mounted HTTP routes (ADR-0250). |
| `mcp/` | The SEP-1649 MCP Server Card that `nova serve` publishes, MCP exchange capture helpers, and the read-only stdio MCP evidence server (`evidence_server.py` over `evidence_tools.py`, **experimental**, `python -m novafabric.mcp.evidence_server --scope DIR`). The evidence server lists, summarizes and verifies capsules offline within the operator's scope directories, using `_capsule_fs` reads that never follow a link and the same `verify_capsule` as `nova verify`. It writes nothing and returns no prompt or response content. Its protocol, privacy, adversarial-input and offline contract is pinned by `tests/mcp_evidence_server/test_conformance_gate.py`. See [the guide](integrations/mcp-evidence-server.md). |
| `ui/dashboard/` (repository root, not under `src/`) | The dashboard frontend — Astro + React, with its own vitest suite. Its dashboard build is copied into `src/novafabric/serve/static/` (`ui/dashboard/scripts/copy-dashboard.mjs`), which ships in the wheel. |
| `metadata_store/` | SQLite and Postgres metadata backends, including row-level security. `dsn.py` is the canonical place a Postgres DSN is normalised — see [below](#one-dsn-two-consumers). |
| `object_capsule_store/` | S3-compatible object storage for capsules at scale. |
| `collector_app/`, `collector/` (repo root, Go), `collector_cffi/` | `collector_app/` is the Python collector (`POST /capsule` into an in-memory DuckDB accumulator, or ClickHouse when `NOVA_CLICKHOUSE_URL` is set). `collector/` is the Go module for the node spool, the spool forwarder, the collector and the verifier (`collector/cmd/`). `collector_cffi/` is the Python binding to the Go spool (with a pure-Python fallback) that `nova capture --emit-spool` writes through (`capture/spool_sink.py`; off by default). |
| `envelope/` | EventEnvelope v1, the per-event wire format (with CloudEvents interop) that the spool carries. |
| `events/` | The opt-in lifecycle event emitter: a local `events.jsonl` and optional webhooks. Nothing is emitted until a sink is configured (`NOVA_EVENTS_LOG` / `NOVA_EVENTS_WEBHOOK`); the default is a no-op sink. |

### Compliance and reporting

`compliance/` is the second-largest module: regulatory exporters (EU AI Act,
ISO 42001, CycloneDX AI-BOM, and others), each mapping capsule facts to a
standard's required form. `report/`, `export_blob/`, `viewer/`, `viz/` handle
presentation and portability.

### Other subsystems

Packages the tables above do not cover. Each maturity label comes from the
package's docstring, its `nova --help` line or the page that documents it;
"library only" means no `nova` command calls it yet.

**Capture runtime, storage and clients**

| Module | What it does | Maturity |
|---|---|---|
| `sdk/` | The `@agent` decorator (`novafabric.sdk.agent`): in-process capture through the same finalization as `nova capture`, without maskers. | works today |
| `daemon/` | The warm capture daemon behind `nova daemon start/stop/status`: a prefork `AF_UNIX` server that forks one isolated worker per run. Linux only, opt-in. See [warm-capture-daemon.md](warm-capture-daemon.md). | experimental |
| `otel/` | OpenTelemetry GenAI spans out (`nova capture --emit-otel-genai`, `otel/genai_emitter.py`) and OTLP in (`nova serve`'s `/api/otlp/v1/traces` and `/logs`, `otel/genai_ingest.py`, `otel/logs_ingest.py`). See [OTLP ingest](architecture/otlp-ingest.md). | experimental |
| `session/` | `nova session`: a content-addressed `session.json` that groups N independent capsules in order, and replays them in sequence. It copies no capsule data. | experimental |
| `jobs/` | Durable background jobs: a SQLite `JobStore` (`$NOVAFABRIC_HOME/jobs.db`) whose workers claim jobs by atomic lease. Used by the server's replay routes (`server/routes/replays.py`). | experimental |
| `ha/` | The writer lease with fencing tokens (`ha/lease.py`). Automated promotion and write-path fencing are not built. | experimental, library only |
| `storage/` | The `StorageBackend` interface (SQLite, Postgres), WORM adapters (local, S3, GCS, Azure Blob), retention and legal-hold primitives, and the dual-object compliance/PII split (`dual_object_store.py`, off by default). | experimental |
| `schemas/` | The packaged JSON Schemas in force at runtime, including `run-capsule.schema.json`, which `nova validate` checks. | works today |
| `migrations/` | Helpers for the registry migration track (`nova db upgrade --track registry`); see [Schema migrations](#schema-migrations). | experimental |
| `capsule_migration/` | `nova migrate`: copies a v0.1.x capsule to a v1.0.0-format copy and appends a `migration_source` lineage edge. The source is never modified. | experimental |
| `import_blob/` | `nova import`: the inverse of `export_blob/`. Verifies, unpacks and re-indexes a batch export, and writes a receipt. | experimental |
| `backup/` | `nova backup create/verify` and `nova restore`: evidence-grade backup sets with a signed coverage table. Key material travels only under an explicit opt-in. | experimental |
| `support_bundle/` | `nova support-bundle`: a redacted, hash-manifested diagnostics tarball, deny-by-default. | experimental |
| `client/` | A typed, synchronous Python client for the server's `/v0` REST API. Server-mode tooling only; no local-mode feature depends on it. | experimental |

**Reading, reporting and governance over runs**

| Module | What it does | Maturity |
|---|---|---|
| `agent_graph/` | `nova graph agent`: a read-only DAG of one run's control flow, rebuilt from `model-calls.jsonl`, `tool-calls.jsonl` and `trace.jsonl`. | experimental |
| `forensics/` | `nova forensics timeline`: a deterministically ordered incident timeline over sealed evidence. Evidence it cannot rebuild is recorded as gaps. | experimental |
| `views/` | `nova view`: saved queries stored as plain files under `.novafabric/views/`. | experimental |
| `trend/` | `nova trend`: an offline cost, score or latency trend report over local capsules, as JSON and optional static HTML. | experimental |
| `dashboards/` | `nova dashboard`: dashboards and widgets stored as portable, versioned JSON files rather than database rows. | experimental |
| `cost/` | `nova cost`: record-only cost estimation, attribution, fairness and roll-up. It never enforces a budget. | experimental |
| `energy/` | `nova energy probe/attest/verify/report`: energy receipts that are measured, or marked unavailable, never invented (`energy-receipts.jsonl`). | experimental |
| `promote/` | The maker-checker seal workflow behind `nova seal propose/approve/verify`: linked Proposal and Approval DSSE envelopes. | experimental |
| `retention/` | `nova retention plan/apply/status/explain`: a WORM-aware retention sweep. `plan` is a dry run. There is no daemon; run it from cron or systemd. | experimental |
| `interop/` | Portable projections: the agent passport (`nova passport`) and Hugging Face Hub revision receipts. | experimental |
| `mcp/`, `mcp_scanner/` | `nova mcp card` (the MCP Server Card at `/.well-known/mcp.json`), `nova mcp conformance`, and the OWASP LLM supply-chain scanner behind `nova mcp scan` and `nova mcp risk-report`. MCP capture itself is in `capture/hooks/_mcp.py` and `proxy/mcp_proxy.py`. | experimental |
| `safetycase/` | The safety-case compiler behind `nova safety-case`: a Claims-Arguments-Evidence tree assembled from existing evidence and carried inside the Evidence Bundle, so it is not a third format. | experimental |

**Record-only evidence facets.** Each of these packages records evidence that
another system produced. None of them decides, enforces or adjudicates anything.

| Module | Records | CLI | Maturity |
|---|---|---|---|
| `hitl/` | Human–agent interaction: hand-offs, overrides, rationale, consent | `nova hitl`, `nova consent` | experimental |
| `frontier_safety/` | Decisions from external control protocols, tripwires and alignment signals | `nova safety` | experimental |
| `federation/` | Cross-org exchanges and pinned foreign trust anchors, plus an offline walk of a transitive trust path | `nova trust-path` | experimental |
| `embodied/` | Sensor and command streams, the operational design domain (ODD) and its excursions, sim-to-real and tele-operation | `nova embodied` | experimental |
| `memstore/` | Changes to an external shared knowledge store. It never hosts the store | `nova memstore` | experimental |
| `preservation/` | Fixity checks, custody hops, seal renewal and format-migration hops | `nova preservation`, `nova migrate-format` | experimental |
| `risk_transfer/` | Insurance, liability and actuarial evidence | `nova insurance` | experimental |
| `settlement/` | The provenance of payments another system made | `nova settlement` | experimental |
| `science/` | Provenance for research and reproducibility, including FAIR RO-Crate | `nova science`, `nova export-rocrate-science` | experimental |
| `context/` | A digest-only manifest of what entered a context window, and the support map a producer claimed | — | experimental, library only |
| `retrieval/` | Where retrieved sources came from (also used by `capture/events.py`) | — | experimental, library only |
| `safety/` | Guardrail decisions that other systems made | — | experimental, library only |

---

## Schema migrations

Two independent Alembic universes (ADR-0211), each with a SQLite and a Postgres
track. `nova db upgrade` drives both; `--track` picks the universe and `--backend`
the database:

| Universe | Versions | Applies to | Command |
|---|---|---|---|
| **MetadataStore** (default) | `src/novafabric/metadata_store/migrations/{sqlite,postgres}/` | The run metadata tier — SQLite at `NOVAFABRIC_METADATA_DB_PATH` (default `$NOVAFABRIC_HOME/metadata.db`), Postgres at `NOVAFABRIC_METADATA_DSN` | `nova db upgrade [--backend postgres]` |
| **Registry** (experimental) | `alembic/sqlite/versions/` (`alembic.ini`), `alembic/postgres/versions/` (`alembic-postgres.ini`) | The registry/server database (`assets`, `eval_results`, `lineage_*`) — SQLite at `$NOVAFABRIC_HOME/registry.db`, Postgres at `NOVAFABRIC_POSTGRES_DSN` | `nova db upgrade --track registry [--backend postgres]` |

From a repository checkout the registry track can also be driven with Alembic directly:

```bash
alembic upgrade head                              # registry, SQLite
alembic -c alembic-postgres.ini upgrade head      # registry, Postgres
```

The MetadataStore Postgres track partitions `runs` quarterly by `started_at` (ADR-0051). Two
consequences are worth knowing before you touch that table:

- **The primary key is `(run_id, tenant_id, started_at)`.** Postgres requires the
  partition column in every unique constraint, so the key must include
  `started_at`; RFC-0001 / ADR-0226 put `tenant_id` back in alongside it, because
  `runs` is the RLS-protected, tenant-scoped table and tenant separation should
  be structural rather than incidental.
- **Idempotency on `(run_id, tenant_id)` lives in the application**, not in a
  constraint. No key on a range-partitioned table can enforce uniqueness on that
  pair alone, so `register_run` guards its insert with `WHERE NOT EXISTS`. If you
  replace that guard with an `ON CONFLICT`, you silently redefine what "the same
  run" means — there is a test that fails when you do.

The repo-root `alembic/` tree is copied into the wheel at build time as
`novafabric/migrations/registry` (`[tool.hatch.build.targets.wheel.force-include]`
in `pyproject.toml`), so an installed CLI can migrate its own registry database
without the repository present.

### One DSN, two consumers

`alembic/env.py` imports `novafabric.metadata_store.dsn.to_sqlalchemy_url` —
migrations depend on the installed package, not the other way round. That
coupling exists because the same connection string means two different things
depending on who reads it:

- `metadata_store.postgres` passes it straight to `psycopg.connect()`, which
  wants a plain libpq URL: `postgresql://user:pass@host:5432/db`.
- Alembic hands it to SQLAlchemy, which resolves the bare `postgresql://` scheme
  to the **psycopg2** dialect. NovaFabric ships `psycopg[binary]` (psycopg 3) and
  does not ship psycopg2.

So the DSN that is correct everywhere else made the documented migration command
fail with `ModuleNotFoundError: No module named 'psycopg2'`. `to_sqlalchemy_url`
rewrites a bare scheme to `postgresql+psycopg://` and leaves an explicitly named
driver (`+asyncpg`, `+psycopg2`) alone — naming a driver is a deliberate choice.

**If you add another consumer of the Postgres DSN, route it through that
function** rather than re-deriving the rule. The MetadataStore Postgres track's
`metadata_store/migrations/postgres/env.py` does (it used to carry its own,
narrower rewrite that missed `postgres://` and overrode an explicit `+psycopg2`;
`tests/metadata_store/test_alembic_env_uses_shared_dsn_rule.py` now pins it to
the shared rule). The bug survived precisely because
each consumer was individually correct.

---

## Design invariants

Break these and the review will ask you to start over — not out of pedantry, but
because each one is load-bearing for a promise the project makes to users.

1. **A capsule is portable and self-describing.** It must verify with no server,
   no network, and no database.
2. **Capture never blocks the user's workload by default.** If a NovaFabric
   component fails, the workload continues and the failure is recorded.
3. **Secrets never leave the redaction boundary.** No prompts, tokens, or env
   vars in logs, telemetry, or unredacted capsule fields.
4. **Full prompt/response capture should be opt-in, never the default.** As
   built, this invariant is **not met**: a default `nova capture` of a Python
   workload writes the request messages and the response choices into
   `model-calls.jsonl` at every capture level (the SDK and wire hooks,
   `capture/hooks/`; `NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off` drops only the
   generic wire hook's response bodies), and mocked replay depends on those
   stored responses. `--capture-content` governs only
   what the OpenTelemetry span export carries. Changing the default is an open
   decision; until then, treat a capsule as holding full prompts and responses.
5. **No silent telemetry and no update checks.** Ever. The project has none and
   will not gain any.
6. **Core local-mode features require no internet.**
7. **Compute-node hot paths never write to a database or graph.** They write to
   a local spool; something else ingests it.
8. **Schema changes are additive and optional first.** A new field must not
   break validation of an existing capsule.
9. **No root containers, no privileged Kubernetes access.**
10. **Two top-level formats only** — Run Capsule and Evidence Bundle. A third is
    not on the table.
11. **Framework structure is introspected by shape, never by class identity**
    (ADR-0250). Enumerating the CLI or the route table goes through
    `cli/introspect.py` or `serve/introspect.py`; no caller writes its own walk,
    and neither uses `isinstance` against a third-party class. Both Typer and
    FastAPI have changed those classes underneath the project, and each time an
    `isinstance` walk reported an empty surface for a system that worked fine.

---

## Deployment modes

| Mode | What runs | Storage | When |
|---|---|---|---|
| **Local** | The CLI only | Filesystem + SQLite | A laptop, a workstation, an HPC login node. The default. |
| **Server** (experimental) | `nova server start` | SQLite or Postgres metadata + a capsule directory on the server | A team sharing capsules, with auth. Single-tenant today. |
| **Cluster-scale** (experimental, partial) | Node spools + spool forwarder + NATS consumers | Local capsules + off-node indexes | Many nodes producing capsules concurrently. |

Every mode reads the same capsule format. Moving up a tier is a deployment
change, not a migration. Component-by-component maturity — including what is
not implemented yet, such as hub signing (future design) and serving capsules from
object storage in server mode — is in [Deployment topologies](architecture/deployment-topologies.md).

---

## What NovaFabric is not

Stated plainly, because knowing the boundary saves you from proposing something
that will be declined:

- **Not an observability platform.** Tracing tells you what happened; this tells
  you whether a run can be replayed, compared, and verified. If you want spans in a
  dashboard, use an APM.
- **Not a hosted service.** There is no SaaS and no account system.
- **Not a model registry or a feature store.**
- **Not a scheduler.** It captures workloads that SLURM or Kubernetes schedules;
  it does not schedule them.

See [the roadmap](../ROADMAP.md) for what is planned, and
[the decisions index](decisions.md) for what has already been decided and why.

---

## Where to go next

| You want to… | Read |
|---|---|
| Understand the concepts | [concepts.md](concepts.md) |
| Make your first change | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Add an asset type, CLI command, or report format | [developer-guide.md](developer-guide.md) |
| Understand the security posture | [SECURITY.md](../SECURITY.md) |
| Propose an architectural change | [RFC process](governance/rfc-process.md) |
| See what has been decided | [decisions.md](decisions.md) |
