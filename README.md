# NovaFabric

[![PyPI](https://img.shields.io/pypi/v/novafabric.svg)](https://pypi.org/project/novafabric/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-beta-yellow.svg)](#status)

**Created and maintained by [Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi)** — AI systems engineer, platform architect, HPC researcher. Part of the [NovaFabric](https://github.com/novafabric) open-source lab.

> **NovaFabric turns commands, scripts, model runs, and AI-agent executions into portable execution capsules:** structured, validated, redacted, and ready for inspection, replay, governance, and future automation.

```bash
# Capture any command — script, agent, training run, experiment
nova capture python my_agent.py

# Validate the resulting capsule against schema
nova validate .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
```

Every captured run produces a `.novafabric/runs/<ulid>/` directory containing a
schema-valid, secret-redacted, portable evidence folder. Works with any command.
No application changes required.

---

## Quick start

### Install

```bash
pip install novafabric
# or with uv:
uv add novafabric
```

NovaFabric requires Python 3.12+.

### Capture a run

```bash
nova capture python my_agent.py --dataset data.csv
```

This produces:

```
.novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
  capsule.yaml          ← run manifest (id, status, timing, refs)
  trace.jsonl           ← execution spans
  model-calls.jsonl     ← LLM API calls (OTel GenAI semconv)
  tool-calls.jsonl      ← tool invocations
  env.lock              ← full environment snapshot
  redaction-proof.json  ← proof no secrets leaked
  replay.yaml           ← replay policy
  inputs/
  outputs/
    stdout.txt
    stderr.txt
```

### Validate a capsule

```bash
nova validate .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
# ✓ Valid capsule: 01HXAY7M5JZ8R7K4P9DPBYK2WX  status=success
```

### Replay a capsule (v0.3)

```bash
# Forensic: read-only inspection, no network, no subprocess
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --mode forensic

# Mocked: re-run command, all model and tool calls served from cache
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --mode mocked

# Dry-run: see what would be mocked before committing
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --dry-run
```

### Diff two capsules (v0.3)

```bash
nova diff .novafabric/runs/01HX.../ .novafabric/runs/01HY.../
# Diff: 01HX... → 01HY...
#   changed=1  added=0  removed=0
# Model calls:
#   ~ call at span 657bff2c61ddad1c

# CI gate: fail if behavior changed
nova diff cap-a/ cap-b/ --assert-no-regressions
```

### Lineage graph (v0.4)

Every capture run automatically emits `lineage.jsonl` with three mechanical edge types
(`consumed`, `produced_by`, `replayed_from`). Query the graph with:

```bash
nova lineage provenance <run-id>          # what this run depended on
nova lineage blast-radius <asset-ref>     # what runs consume this asset
nova lineage replay-chain <run-id>        # replay ancestry
```

### SDK decorator (in-process capture)

```python
from novafabric.sdk.agent import agent

@agent(name="research-agent", version="0.1.0", capsule_dir="capsule/")
def run():
    # openai, anthropic, httpx calls are auto-captured
    response = client.chat.completions.create(...)
    return response

run()
```

The `capsule_dir` parameter is optional. Without it, the decorator behaves as
in v0.1 — OTel spans only, no capsule written.

---

## How it works

`nova capture <cmd>` injects a `sitecustomize.py` loader into the subprocess
via `PYTHONPATH`, which installs monkey-patches for:

- **Per-SDK hooks** — `openai.resources.chat.completions.Completions.create`,
  `anthropic.resources.messages.Messages.create`,
  `mcp.client.session.ClientSession.call_tool`
- **Wire-level hooks** — `httpx.Client.send`, `requests.Session.send`,
  `aiohttp.ClientSession._request`, `urllib3.HTTPConnectionPool.urlopen`
  — URL-classified via a vendored registry (OpenAI, Anthropic, Cohere,
  Together, Mistral, Replicate, AWS Bedrock; user-extensible at
  `~/.novafabric/url_registry.yaml`)
- **Layering guard** — when `requests` calls go through `urllib3`
  internally, exactly one record is produced (not two) — see
  [ADR-0025](design/adr/) and v0.6.0 release notes
- **Body adapters** — Bedrock-Anthropic / Cohere / Titan / Llama bodies
  are normalized into OpenAI shape so `gen_ai.request.model` populates
  correctly across all providers
- **OTel GenAI semconv** — every `gen_ai.*` field defined as "Required
  when applicable" is extracted: temperature, top_p, top_k, max_tokens,
  stop_sequences, seed, frequency_penalty, presence_penalty,
  response.id, finish_reasons

All patches are removed after the run. If an SDK is not installed, its hook is
silently skipped. Capture works even if none of the AI SDKs are present.

For **non-Python clients** (Claude Code, Cursor, Continue, Node/Go agents),
two transparent HTTP proxies provide the same capture without modifying
the client:

- **`nova api-proxy`** — captures LLM API calls (point your client at
  `http://127.0.0.1:8765` via `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`).
  Streaming responses are merged into a synthesized non-streaming
  envelope for the record.
- **`nova mcp-proxy`** — captures MCP tool exchanges (stdio transport
  for Claude Desktop / Cursor; HTTP/SSE transport for HTTP MCP servers).

Both proxies auto-allocate a capsule directory if `--capsule-dir` is omitted.

Secret scanning runs against every artifact before the capsule is finalized.
Detected values are redacted in-place; a cryptographically chained proof record
is written to `redaction-proof.json`.

---

## What is captured

| Artifact | Contents |
|---|---|
| `capsule.yaml` | Run id (ULID), status, command, timing, artifact refs |
| `trace.jsonl` | Root span + any child spans (OpenTelemetry-compatible) |
| `model-calls.jsonl` | One record per LLM call, OTel GenAI semconv fields |
| `tool-calls.jsonl` | Tool invocations (populated by future tool hooks) |
| `env.lock` | Python version, packages, OS, CPU, GPU, locale, env vars |
| `redaction-proof.json` | Scan summary, findings count, chain hash |
| `replay.yaml` | Replay mode and constraints |

Capsules are written on **success and failure**. A failed run produces a
complete capsule with `status: failure`, `exit_code: N`, and an `error` block.

---

## Asset registry (v0.1, included)

NovaFabric also ships the **Asset Registry** — a local SQLite registry for
AI assets:

```bash
nova register my-model.yaml
nova list --type agent
nova inspect my-agent@1.0.0
nova promote direct my-agent@1.0.0 --to staging   # v0.13+: promote is a sub-group
nova eval my-agent@1.0.0
nova diff my-agent@1.0.0 my-agent@1.1.0
nova report
nova validate spec.yaml   # asset spec or capsule directory
```

See [`docs/getting-started.md`](docs/getting-started.md) for the full registry walkthrough.

---

## When to use NovaFabric

Use NovaFabric when you need to:

- **Reproduce an AI run later** — replay a captured agent or model run for regression
  debugging or incident forensics, instead of guessing what changed.
- **Diff two runs** — see exactly which model calls, tool calls, or outputs changed
  between yesterday and today, and gate CI on behavioral change.
- **Produce portable, signed evidence** of what an agent or model actually did —
  for governance, auditability, and compliance *support*.
- **Capture without changing application code** — SDK hooks, wire-level hooks, and
  transparent proxies capture runs of any command, fully **local and offline**.

### When *not* to use NovaFabric

Be honest about the trade-offs — NovaFabric is **not** the right tool when:

- You want a fully managed, hosted observability dashboard with zero operations —
  a SaaS LLM-observability platform will be less work.
- You need large-scale, real-time, multi-user team analytics **today** — server mode
  and the live dashboard are `experimental`.
- You need a compliance *certification* — NovaFabric produces evidence that
  *supports* compliance workflows; it does not certify or guarantee compliance.
- You need frozen, long-term-stable on-disk formats right now — the Run Capsule and
  Evidence Bundle formats are not frozen until the v1.0 schema freeze.

---

## How NovaFabric compares

NovaFabric overlaps with LLM-observability platforms but is centered on a different
unit of value: a **portable, signed, replayable evidence capsule** rather than a
trace in a hosted database.

| | **NovaFabric** | Self-hosted observability (Langfuse, Arize Phoenix) | Hosted SaaS (LangSmith, W&B, Helicone) |
|---|---|---|---|
| Deployment | Local-first CLI, **no account** | Self-hosted server + database | Managed cloud service |
| Primary artifact | Portable evidence **capsule** (a folder) | Trace row in a database | Trace row in a vendor cloud |
| Where data lives | **On your machine** | Your server | Vendor cloud |
| Replay of a run | **✓ 4 modes** (exact / mocked / semantic / forensic) | ✗ | ✗ |
| Run-to-run structural diff | **✓** | partial (eval) | partial (eval) |
| Cryptographic signing / provenance | **✓** in-toto DSSE + Sigstore + RFC 3161 | ✗ | ✗ |
| Capture without code changes | **✓** SDK + wire-level + proxy | SDK instrumentation | SDK / proxy |
| Works fully offline | **✓** | self-host only | ✗ |

**Where the alternatives are stronger:** hosted and self-hosted observability
platforms offer richer turnkey dashboards, managed evaluation UIs, and team
analytics out of the box. NovaFabric trades that for portability, offline
operation, and signed, replayable artifacts you own. They are complementary —
NovaFabric emits OpenTelemetry GenAI and OpenLineage, so you can feed an existing
observability stack while keeping portable capsules for replay and audit.

See [`docs/concepts.md`](docs/concepts.md) for the five primitives and four replay
modes in depth.

---

## Roadmap

> All shipped items (`✓`) are `experimental` unless marked `prototype`. Interfaces and
> formats may change until the v1.0 schema freeze. See [`ROADMAP.md`](ROADMAP.md) for
> per-feature maturity labels.

```text
v0.1  ✓  Asset Registry — SQLite, 8 CLI commands, eval-gated promotion
v0.2  ✓  Execution Capsules + Agent Capture + Capsule Validation
v0.3  ✓  nova replay (forensic + mocked) + nova diff (structural)
v0.4  ✓  Lineage graph + retroactive import + OpenLineage export
v0.4  ✓  Trust layer — scan-secrets, redact, export-evidence
v0.5  ✓  MCP capture (hook + stdio proxy) + plugin entry-point contract
v0.6  ✓  Wire-level expansion (aiohttp + urllib3 + Bedrock), body adapters, full OTel GenAI semconv
v0.6  ✓  Multi-target runners (local + Docker + Kubernetes + Slurm)
v0.6  ✓  nova api-proxy + nova mcp-proxy (HTTP/SSE) for non-Python clients
v0.7  ✓  Server mode (multi-tenant REST API, OIDC, RBAC, offline tokens)
v0.8  ✓  Policy + approval gates (OPA/Rego, maker-checker, WORM storage adapters, legal holds)
v0.9  ✓  Standard eval suites (GAIA, AgentBench, SWE-bench, MMLU, Smoke; OCI-pinned; Rego-gated)
v0.10 ✓  NovaSeal — DSSE signing (ECDSA P-256), RFC 3161 timestamps, Merkle log, nova verify
v0.10 ✓  Event Envelope v1 — canonical wire format (JSON Schema + proto3 + sha256 pin)
v0.10 ✓  Cluster-scale collector tier — Go binary, crash-safe spool (100-SIGKILL recovery tested)
v0.10 ✓  Object Capsule Store — SHA-256 CAS, multi-backend router (local/S3/MinIO), WORM conformance
v0.10 ✓  Metadata DB — Postgres RLS, multi-tenant isolation, PgBouncer support, nova db
v0.10 ✓  Parent/Child Capsule — PARENT + WORKER hierarchy, PARTIALLY_COMPLETE state [prototype]
v0.10 ✓  Lineage at Scale — KuzuDB v2 backend, benchmark harness, migration kit [experimental];
          federation protocol [prototype — OQ-04 sovereignty open]
v0.11 ✓  Dashboard Completeness — every CLI capability has a dashboard equivalent (13 tabs, DC-1..DC-8)
v0.12 ✓  Asset Intelligence — nova rollback, nova unregister, nova suggest-register,
          stale detection, dependency graph, --require-asset-status gate
v0.13 ✓  Maker-Checker dual-approval (D-5) — nova promote sub-app (direct/propose/approve),
          Ed25519 keyring, N-run diff in dashboard
v0.14 ✓  NovaSeal linked-envelope chain maker-checker + SealTab + RBAC API (role mgmt REST);
          security & CI hardening (10 Dependabot alerts cleared)
v0.15 ✓  Compliance evidence MVP — cap-001/002/004/005 (ToolPermission, AnnexIV, NIS2, PIIDetect);
          Track B dashboard scale (cursor pagination + SSE live feed)
v0.16 ✓  Governance + audit + judge + adapters + HPC runners; GovernanceTab UI;
          Live Topology Dashboard (Track C, 2D Sigma.js + Arrow IPC + DeltaBuffer)
v0.17 ✓  Evidence Fabric v1.0 (cap-001/002/003/004/006/009) + Capsule KG v1 (KuzuDB) +
          TV-5 3D Topology View (Three.js, nova serve --tv5); 3 parallel tracks
v0.18 ✓  Dashboard parity for v0.17 — KGTab + capture-level + GDPR erasure + storage panels;
          8 new serve endpoints; v0.11 completeness principle restored
v0.19 ✓  Complete dashboard parity — CostTab + SchemaTab; all 7 v0.17 CLI surfaces now have
          dashboard equivalents; tutorial sections added for KG/capture-level/erasure/TV-5
v1.0     OAS v1.0 schema freeze + production-ready governance [planned]
```

See [`CHANGELOG.md`](CHANGELOG.md) for release-by-release details.

---

## FAQ

**What is NovaFabric?**
An open-source, local-first CLI toolkit that captures, replays, diffs, and audits
AI agent and model runs as portable, redacted evidence capsules. It is built around
five primitives: Asset Registry, Run Capsule, Replay, Lineage, and Evidence Bundle.

**Is it free and open source?**
Yes — Apache-2.0 licensed. There is no paid tier or hosted service required.

**Does NovaFabric send my data anywhere?**
No. NovaFabric is local-first: captured data stays on your machine. There are no
accounts and no telemetry, and core features (capture, validate, replay, diff,
lineage) work fully offline.

**Do I have to change my code to use it?**
No. `nova capture <command>` captures any command with no application changes.
Python SDKs (OpenAI, Anthropic, MCP, httpx, requests, aiohttp, urllib3, Bedrock)
are auto-hooked; non-Python clients are captured via `nova api-proxy` and
`nova mcp-proxy`.

**What is an "evidence capsule"?**
A portable `.novafabric/runs/<ulid>/` folder containing a schema-valid,
secret-redacted record of a run: the manifest, traces, model/tool calls, the
environment lock, a redaction proof, and a replay policy.

**Can I replay a captured run?**
Yes — four modes: `exact`, `mocked`, `semantic`, and `forensic` (read-only, no
network, no subprocess).

**How is this different from LangSmith / Langfuse / W&B?**
Those are observability platforms centered on traces in a (hosted or self-hosted)
database. NovaFabric is local-first and centered on portable, signed, *replayable*
capsules you own, with run-to-run structural diff and cryptographic provenance.
See [How NovaFabric compares](#how-novafabric-compares).

**Is NovaFabric production-ready?**
It is **beta** (v0.58.0). Local capture, replay, diff, lineage, the trust layer,
policy gates, eval suites, and the asset registry are usable; server mode, the
cluster-scale collector, and the dashboard are `experimental`. On-disk formats are
not frozen until the v1.0 schema freeze.

**What Python version is required?**
Python 3.12 or newer.

**How do I cite NovaFabric?**
See [Citation](#citation) below, or the [`CITATION.cff`](CITATION.cff) file.

---

## Documentation

### For users
- [Getting Started](docs/getting-started.md)
- [Concepts](docs/concepts.md)
- [CLI Reference](docs/cli-reference.md)
- [Python API](docs/python-api.md)
- [Architecture](design/architecture/overview.md)

### For the curious
- [Vision: North Star](design/strategy/north-star.md)
- [Vision: Replayable AI Infrastructure](design/strategy/replayable-ai-infrastructure.md)
- [Strategy: Non-Goals](design/strategy/non-goals.md)
- [Strategy: Agentic Research-to-Production OS](design/strategy/agentic-research-to-production-os.md)

### Release notes
- [v0.19.0 — Complete dashboard parity](docs/releases/v0.19.0.md)
- [v0.18.0 — Dashboard parity for v0.17.0](docs/releases/v0.18.0.md)
- [v0.17.0 — Evidence Fabric v1.0 + Capsule KG + TV-5 3D](docs/releases/v0.17.0.md)
- [v0.10.0 — NovaSeal Cryptographic Core](docs/releases/v0.10.0.md)
- [v0.9.0 — Standard Eval Suites](docs/releases/v0.9.0.md)
- [v0.8.0 — Policy + Approval Gates](docs/releases/v0.8.0.md)
- [v0.7.0 — Server Mode](docs/releases/v0.7.0.md)
- [v0.4.0 — Lineage Graph](docs/releases/v0.4.0.md)
- [v0.3.0 — Replay and Diff](docs/releases/v0.3.0.md)
- [v0.2.0 — Execution Capsules](docs/releases/v0.2.0.md)
- [v0.1.0 — Asset Registry](docs/releases/v0.1.0.md)

### For contributors
- [Contributing](CONTRIBUTING.md)
- [Developer Guide](docs/developer-guide.md)
- [Governance](GOVERNANCE.md)

---

## Developer setup

```bash
git clone git@github.com:novafabric/novafabric.git
cd novafabric
uv sync --dev
uv run pytest
uv run ruff check src tests
uv run mypy src
```

Requirements: [uv](https://docs.astral.sh/uv/).

---

## Status

**Beta — actively developed (v0.58.0).** Stable and usable today: local capture,
replay, diff, lineage (SQLite), the trust layer (signing, secret scanning,
redaction), the asset registry, policy/approval gates, and standard eval suites.
`Experimental`: server mode, the cluster-scale collector, the Object Capsule Store,
and the live dashboard (see [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md)
for per-feature maturity labels and the authoritative release history). Run Capsule
and Evidence Bundle formats are **not frozen** — expect schema changes until the
v1.0 freeze. NovaFabric produces evidence that *supports* compliance workflows; it
does not certify or guarantee compliance.

---

## Standards adopted

OpenTelemetry GenAI semconv · Anthropic MCP · OpenLineage · in-toto · SLSA ·
Sigstore · JSON Schema 2020-12 · OCI · OPA/Rego · NIST AI RMF

---

## Citation

If you use NovaFabric in your research or tooling, please cite it. Citation
metadata lives in [`CITATION.cff`](CITATION.cff); a BibTeX entry:

```bibtex
@software{novafabric,
  author  = {Seyedkazemi Ardebili, Mohsen},
  title   = {{NovaFabric}: Replayable AI Infrastructure},
  url      = {https://github.com/novafabric/novafabric},
  version = {0.58.0},
  license = {Apache-2.0}
}
```

---

## License

Apache-2.0
