# Why NovaFabric? A plain-English guide

> **Who this is for:** engineers who have heard "AI observability" but aren't sure
> why they need something more — or what a reproducibility layer actually buys them
> day-to-day.

**What you will learn**

- The one problem NovaFabric exists to solve: AI and agent runs are non-reproducible
  and leave no durable evidence.
- The five primitives — **Asset Registry, Run Capsule, Replay, Lineage, Evidence
  Bundle** — and the verb chain that connects them: **Capture → Seal → Replay → Diff
  → Audit**.
- Five concrete things you can do with the `nova` CLI that you could not do before,
  each with a runnable example.
- How the pieces fit together, how to capture without changing your agent code, and
  where NovaFabric stops (what is shipped today vs. what is still planned).

Everything below runs locally, offline, with no accounts and no telemetry. NovaFabric
is local-first, open-source (Apache-2.0), and has shipped v0.1 through v0.9. Where a
capability is design intent rather than code, this guide labels it **PLANNED** — it
never claims a roadmap item as done.

---

## The core problem: AI runs have no memory of themselves

When a traditional program runs, you have logs, stack traces, and (usually)
deterministic behavior. When an AI agent or model workload runs, you get output — and
then it's gone.

Two weeks later someone asks:

- "Why did the agent diagnose that SLURM job as an OOM failure instead of an MPI
  error?"
- "Did the agent's recommendations change after we updated the system prompt?"
- "Which analyses do we need to re-run now that we found a corrupt sensor dataset?"

Without infrastructure, the answer to all three is: **"I don't know."**

The relevant standards for answering these questions already exist — OpenTelemetry
GenAI semantic conventions for model calls, OpenLineage for pipeline lineage, MCP for
tool calls, in-toto and SLSA for build provenance — but they live as fragments. No
single tool unifies them into a developer-friendly replay fabric for complete AI
systems.

That is what NovaFabric does. Every run becomes a **Run Capsule** — a portable,
schema-valid, secret-redacted record of exactly what happened, written on both success
*and* failure, that you own and can read months later. The product thesis in one line:

> Tracing tells you *what happened*. NovaFabric tells you whether a past run can be
> safely **replayed**, **compared**, and **proven**.

---

## The five primitives at a glance

| Primitive | What it is | Since |
|---|---|---|
| **Asset Registry** | Local SQLite registry of versioned assets (`name@version`), pinned to a git SHA, with a six-state lifecycle | v0.1 |
| **Run Capsule** | The unit of capture: a ULID-named directory holding every observable fact of one execution | v0.2 |
| **Replay** | Re-execute or inspect a capsule under control, in four honest modes: `forensic`, `mocked`, `semantic`, `exact` | v0.3 |
| **Lineage** | A directed provenance graph (SQLite) derived from capsules: `provenance`, `blast-radius`, `replay-chain`, `time-travel` | v0.4 |
| **Evidence Bundle** | A signed, self-contained ZIP an auditor can verify offline with only `sha256sum` + an ed25519 verifier | v0.4 |

Cryptographic sealing is part of the Evidence Bundle / trust layer, not a sixth
primitive. The strategic verb chain across all five is **Capture → Seal → Replay →
Diff → Audit**.

---

## Five things you can do that you couldn't do before

### 1. Capture — "What exactly happened?"

**The situation:** Your IoT anomaly-detection agent files a report: "vibration sensor
spiked at 14:32, probable bearing failure." Three months later, maintenance disputes
it. What code ran? What data did the model actually see? What did it output before
writing the report?

**Without NovaFabric:** Gone. You have the report file and nothing else.

**With NovaFabric:**

```bash
nova capture python workloads/iot_detective/agent.py
```

No code changes are required. `nova capture` injects a `sitecustomize.py` over
`PYTHONPATH` and installs monkey-patches that record the full run as a capsule: model
name, temperature, `top_p`, `seed`, every prompt sent, every response received, every
tool call and its result, wall-clock timing, and exit code. Model calls are written in
OpenTelemetry GenAI semantic-convention form (`gen_ai.*`). A capsule is written even on
failure (with `status: failure` and an `error` block), and even if no AI SDK is
present.

A capsule is a directory identified by a ULID (a time-sortable ID). Inside it you'll
find, among other files:

| File | Contents |
|---|---|
| `capsule.yaml` | The manifest |
| `trace.jsonl` | OpenTelemetry spans |
| `model-calls.jsonl` | LLM calls in OTel GenAI semconv |
| `tool-calls.jsonl` | Tool invocations and results |
| `env.lock` | Python version, packages, safe env vars, OS/arch/CPU/mem, GPU presence |
| `redaction-proof.json` | Proof that a secret scan ran before the capsule was finalized |

Validate and read it:

```bash
nova validate capsules/01KR9Q2AD…       # check the capsule is well-formed
cat capsules/01KR9Q2AD…/model-calls.jsonl  # read the recorded LLM calls
```

A secret scanner redacts in place (`[REDACTED:rule-id]`) and writes
`redaction-proof.json` before finalize. A capsule missing that proof is **invalid** to
`nova validate` and cannot be exported — redaction is enforced, not optional.

Three months later, you open the capsule and read exactly what happened.

---

### 2. Diff — "Did the agent's behavior change?"

**The situation:** You updated the `hpc-analyst` system prompt to be more concise. Did
the actual diagnoses change? Are the tool calls the same? Does it still cite the right
log lines?

**Without NovaFabric:** You'd have to run it again and eyeball the output manually.

**With NovaFabric:**

```bash
nova diff capsules/01KR9Q2A…  capsules/01KRB4F7…
```

`nova diff` aligns the model and tool calls across the two runs and reports a
structured comparison:

```
model:          same     (qwen3:35b)
tools_called:   same     (read_log, write_diagnosis)
tool_args:      CHANGED — read_log now reads 40 lines, was 100
output_length:  CHANGED — 312 tokens → 180 tokens
classification: same     (OOM)
```

You can see immediately whether the prompt change caused a behavioral regression —
even if the final label looks the same.

Wire it into CI as a gate:

```bash
nova diff --assert-no-regressions capsules/baseline… capsules/candidate…
# exits non-zero (1) on a regression, so the pipeline fails
```

This is the "worked yesterday, fails today" flaky-agent problem turned into a check
your CI can enforce.

---

### 3. Replay — "Can I reproduce this result?"

**The situation:** The HPC team disputes a diagnosis from last Tuesday. They say the
agent hallucinated a line number. You want to prove the output is reproducible — or
find out if something drifted.

**Without NovaFabric:** You'd have to hope the same log file is still there, hope the
model hasn't changed, and re-run manually.

**With NovaFabric:** Replay has four honest, falsifiable modes — pick the one that
matches what you're trying to establish:

| Mode | What it does | Use it for |
|---|---|---|
| `forensic` | Read-only inspection, no subprocess, no network | Audit / post-incident |
| `mocked` | Re-spawns the command; LLM calls served from the capsule cache; tool calls gated by a safety ladder | CI / regression |
| `semantic` | Re-executes and judges *meaning*, not tokens (0.0–1.0 similarity) | Drifting remote LLMs |
| `exact` | Byte-exact, requires a deterministic env and per-call seed | Local / on-prem / compliance |

For the incident above, start read-only:

```bash
nova replay --mode forensic capsules/01KR9Q2AD…
```

This inspects the *exact same inputs* stored in the capsule — same log content, same
model config, same recorded tool responses — with no subprocess and no network. To
re-drive the command with cached LLM responses, use `--mode mocked`. If the agent
produces the same diagnosis, it's reproducible; if not, something drifted (a model
update, a tool behavior change, or non-determinism).

> **Honesty note:** NovaFabric does **not** claim byte-exact replay of *remote* LLM
> calls. `exact` mode is for deterministic local/on-prem execution; for drifting remote
> models, `semantic` mode judges meaning instead of tokens.

A replay is itself a new capsule — so you can `nova diff` the original against the
replay to see precisely what moved.

---

### 4. Lineage — "Which runs are affected by this data problem?"

**The situation:** You discover that `slurm-logs/job-42819-oom@v1` contained truncated
output — the last 200 lines were missing. Which agent runs read that file? Which
diagnoses might be wrong?

**Without NovaFabric:** You'd grep through logs hoping something is recorded, or just
assume everything is suspect.

**With NovaFabric:** Each agent declares what it consumed, and NovaFabric builds a
provenance graph from those declarations:

```python
# inside hpc_analyst/agent.py
record_consumed("slurm-logs/job-42819-oom@v1")
```

The lineage graph is a SQLite cache derived from each capsule's `lineage.jsonl`, with
mechanical edge types (`consumed`, `produced_by`, `replayed_from`) and two confidence
levels (`observed` at runtime vs. `inferred` from structure). Query the blast radius —
every run downstream of the bad file:

```bash
nova lineage blast-radius slurm-logs/job-42819-oom@v1
# → run:01KR9Q2AD…  run:01KRB4F7…  run:01KRC3X9…
```

You now re-run exactly those capsules with the corrected log — no guesswork, no
"assume everything is suspect." Related queries: `provenance` (ancestors),
`replay-chain`, and `time-travel`.

NovaFabric also emits OpenLineage 2.0.2 events (START / COMPLETE / FAIL), so the same
lineage can flow into Marquez, Atlan, or OpenMetadata.

---

### 5. Evidence Bundle — "Prove this was done correctly"

**The situation:** An auditor asks you to demonstrate that your ML experiment reviewer
agent actually read the baseline CSV before making a deployment recommendation — and
that the output hasn't been tampered with since.

**With NovaFabric:**

```bash
nova export-evidence capsules/01KRB4F7… --output bundle.zip --key ed25519.pem
```

> `--output`/`-o` and `--key` (a PEM-encoded ed25519 private key) are both
> **required** — the command exits non-zero without them.

This produces a signed, self-contained ZIP that embeds the capsule, a lineage
subgraph, in-toto DSSE attestations, ed25519 signatures, and vendored JSON schemas.
The key property: it is verifiable with only `sha256sum` plus an ed25519 verifier —
**no NovaFabric runtime required** — so an auditor can check the signature and read the
exact chain of events entirely offline.

> **Scope caveat (honest):** an Evidence Bundle attests that a capsule is *unmodified
> since signing*. It does not certify regulatory compliance or vouch for the content
> itself. **Corrected 2026-07-30:** RFC 3161 trusted timestamps and an append-only
> Merkle transparency log are not merely planned — **NovaSeal** (`trust/novaseal/`,
> ADR-0041/ADR-0070) implements both, plus DSSE signing, and is **experimental**
> and opt-in on the `nova capture` / `nova verify` path (`tests/seal/` passes,
> including a p99 latency gate). Ed25519-signed Evidence Bundles plus in-toto DSSE
> attestations remain the stable default; NovaSeal is the richer, still-evolving
> layer on top, not a separate unbuilt service.

---

## How it fits together

```
         nova capture
              │
              ▼
         [Run Capsule]  ←── portable, redacted record of one run
              │
       ┌──────┼───────────────┐
       ▼      ▼               ▼
   validate  diff          replay
              │               │
              │         [replay capsule]  ←── itself diffable
              │
         lineage (from lineage.jsonl)
              │
              ▼
      [lineage graph]  ←── which runs consumed which assets
              │
              ▼
      nova export-evidence
              │
              ▼
     [Evidence Bundle]  ←── signed ZIP, verifiable offline
```

The design invariant behind this picture: **the capsule is the source of truth**. The
Asset Registry, the lineage graph, and (in server mode) the metadata database are all
*derived, rebuildable indexes* — you can throw them away and rebuild them from
capsules.

---

## Capture approaches

You don't have to change your agent code to use NovaFabric. There are four ways to
capture, all shipped:

| Approach | When to use | Example |
|---|---|---|
| `nova capture` | You control how the process starts | `nova capture python agent.py` |
| `nova api-proxy` | Agent already running as a service, or non-Python | transparent HTTP proxy, URL-classified against a vendored provider registry |
| `nova mcp-proxy` | Agent uses MCP tools over stdio | `nova mcp-proxy -- python mcp_server.py` |
| In-process hooks / `@novafabric.agent` | Jupyter notebook, embedded agent | decorate the function or install hooks in-process |

Under the hood, `nova capture` covers both SDK-level hooks (OpenAI, Anthropic) and
wire-level hooks (httpx, requests, aiohttp, urllib3, Bedrock, and MCP) — so capture
works whether or not your code uses a vendor SDK directly. Third-party plugins are
auto-discovered via the `novafabric.hooks` entry-point group.

---

## Where NovaFabric runs

The same five primitives work from a laptop to a cluster:

- **Local mode (default):** SQLite for the registry (`~/.novafabric/registry.db`) and
  the lineage cache. No network required — capture, validate, replay, diff, and
  lineage all work offline and air-gapped.
- **Multi-target runners (shipped, v0.6):** Local, Docker, non-privileged Kubernetes
  Job, and Slurm (`sbatch` + `sacct`).
- **Server mode (shipped, experimental, v0.7):** a FastAPI service backed by Postgres,
  with OIDC, RBAC (`reader < writer < admin`, plus an orthogonal `auditor`), and
  offline CI tokens. Local mode never *requires* server mode.
- **`nova serve --experimental` (v0.7):** a local-only dashboard that binds
  `127.0.0.1`, uses a one-shot token, and shows the equivalent CLI command on every
  page. The CLI + JSON remain the canonical interface through v1.0.

> **Corrected 2026-07-30 — this used to be accurate, it no longer is.** The
> cluster-scale collector, parent/child capsules for distributed runs, an object
> capsule store, a production metadata store with row-level security,
> lineage-at-scale, and a live topology dashboard are **shipped, as
> experimental** (not planned-only): Collector (Go + Python, Phase 2, v0.14.3),
> parent/child capsules (Phase 3, v0.15.0), Object Capsule Store (Phase 4,
> v0.14.5), Metadata DB with Postgres RLS (Phase 5, v0.14.6), and all four
> lineage-at-scale backends — KuzuDB, Postgres, Apache AGE, JanusGraph — (Phase
> 6, complete as of v0.70.0). `nova serve --topology` (the live topology
> dashboard, ADR-0068) is also shipped experimental. What is genuinely still
> design intent only: cross-cluster **federation** and full cross-org identity
> (Phase 6 of `design/architecture/cluster-scale.md`'s federation layer, not to
> be confused with the lineage-backend Phase 6 above). See
> `docs/tutorials/cluster-scale.md` for the full, corrected status table.

---

## The testbench in one sentence

The `nova-testbench` runs four real agents continuously — HPC failure analyst, ML
experiment reviewer, IoT anomaly detector, incident triage — so that live capsules,
real diffs, and a growing lineage graph are always available to explore. It is the
simplest proof that the whole pipeline works end to end.

```bash
make nova-loop       # run all agents in a continuous loop
make nova-dashboard  # open the local dashboard
```

---

## When does NovaFabric pay off?

The system earns its value the first time you hit one of these:

| Question | Command | Payoff |
|---|---|---|
| "Why did this agent output change?" | `nova diff` | Structured, aligned comparison in seconds |
| "Which runs must I re-validate after a data update?" | `nova lineage blast-radius` | Exact downstream set, not "everything is suspect" |
| "Can I reproduce this result from months ago?" | `nova replay --mode forensic` | Read-only re-drive against the stored inputs |
| "Prove to the auditor what happened." | `nova export-evidence` | Signed ZIP, verifiable offline with `sha256sum` + a verifier |

Without NovaFabric, each of those questions costs hours of forensic work — if it's
answerable at all.

---

## Next steps

- **Capture your first run:** `nova capture python your_agent.py`, then
  `nova validate` the resulting capsule.
- **Make it a CI gate:** add `nova diff --assert-no-regressions` between a baseline and
  candidate capsule.
- **Trace impact:** declare what your agents consume, then use
  `nova lineage blast-radius` when data changes.
- **Produce audit evidence:** `nova export-evidence` and hand the signed bundle to a
  reviewer.
- **Go deeper:** see the CLI reference in `docs/` for the full command surface, and the
  Run Capsule and Evidence Bundle schemas for the on-disk formats (not frozen until the
  v1.0 schema freeze — treat them as experimental).
