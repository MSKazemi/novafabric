# Concepts

---

## Run Capsule

A **run capsule** is the fundamental unit of capture in NovaFabric. It is a
directory containing all observable facts about a single command execution:
the command, timing, environment, every LLM call made, every tool invoked,
stdout/stderr, and a proof that no secrets escaped.

Capsules are identified by a [ULID](https://github.com/ulid/spec) — a
lexicographically sortable, time-prefixed unique identifier.

```
.novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
  capsule.yaml          ← run manifest
  trace.jsonl           ← execution spans
  model-calls.jsonl     ← LLM API calls
  tool-calls.jsonl      ← tool invocations
  assets.jsonl          ← asset references
  env.lock              ← environment snapshot
  redaction-proof.json  ← secret scan proof
  replay.yaml           ← replay constraints
  lineage.jsonl         ← lineage edges
  inputs/
  outputs/
    stdout.txt
    stderr.txt
  .seal/                ← [optional] NovaSeal cryptographic seal (v0.10)
    manifest.dsse       ← DSSE envelope (ECDSA P-256 signature + cert)
    manifest.dsse.tsr   ← RFC 3161 timestamp response (DER)
    log-entry.json      ← Merkle log leaf + inclusion proof
```

Capsules are written on success **and** failure. A failed run produces a
complete capsule with `status: failure` and an `error` block.

---

## Capture Hook Mechanism

`nova capture <cmd>` works without modifying application code. It injects a
`sitecustomize.py` loader into the subprocess via `PYTHONPATH`. When Python
starts the subprocess, it imports this loader automatically, which installs
monkey-patches for:

- **OpenAI** — `openai.resources.chat.completions.Completions.create`
- **Anthropic** — `anthropic.resources.messages.Messages.create`
- **httpx** — `httpx.Client.send`, recording requests classified by the URL registry (`src/novafabric/capture/hooks/url_registry.yaml` + `~/.novafabric/url_registry.yaml` override). Default coverage: OpenAI, Anthropic, Cohere, Together, Mistral, Replicate, AWS Bedrock, Ollama (default port 11434). Non-default Ollama ports are detected automatically from `OLLAMA_BASE_URL` / `OLLAMA_HOST` at call time.
- **requests** — `requests.Session.send`, same URL-registry classification (v0.5; RFC-0001 Option C wire-level layer). Covers LangChain HTTP adapters, LlamaIndex REST clients, and any SDK that ships over `requests`.
- **aiohttp** — `aiohttp.ClientSession._request`, async wire-level capture (v0.6 / C-3.1). Catches LangChain async paths, FastAPI agents, streaming-first SDKs.
- **urllib3** — `urllib3.connectionpool.HTTPConnectionPool.urlopen`, lowest-tier wire-level capture (v0.6 / C-3.2). Sits below `requests` and `boto3`; transitively covers AWS Bedrock and any SDK that uses `urllib3` directly. Layered with the higher hooks via a context-local guard so a single `requests` call records exactly once.
- **MCP (Model Context Protocol)** — `mcp.client.session.ClientSession.call_tool`, recording every tool invocation as a `tool-calls.jsonl` entry with `transport=mcp` and a synthesized JSON-RPC envelope (v0.5; ADR-0015 §Primary).
- **Third-party plugins** — any class registered under the `novafabric.hooks` entry-point group is auto-discovered alongside the built-ins (v0.5.x experimental; RFC-0001 §Detailed design — Option C). Plugin authors: see [`docs/integrations/writing-a-hook-plugin.md`](integrations/writing-a-hook-plugin.md).

### Runners (where the captured workload executes)

The orchestrator delegates subprocess execution to a **runner** (ADR-0025).
Choose one with `nova capture --runner <name>`:

- **`local`** (default) — runs the workload as a local subprocess. All
  v0.5.x behavior; no remote infrastructure needed.
- **`docker`** (v0.6) — runs the workload inside a Docker container.
  Requires `--runner-option image=<ref>`; the image must already have
  NovaFabric installed (e.g. `pip install novafabric` in your Dockerfile).
  Capsule directory is mounted as a volume so artifacts land on the host
  filesystem. Other options: `network`, `workdir`, `user`, `extra_volumes`,
  `extra_env`. No privileged containers, no docker-socket mounts, no host
  namespaces by default — see ADR-0025 §Anti-patterns.
- **`kubernetes`** (v0.6.1) — runs the workload as a Kubernetes `Job`
  via `kubectl` shell-out. Required options: `image`, `namespace`. Optional:
  `service_account`, `node_selector`, `resources`. Capsule artifacts are
  pulled back via `kubectl cp` after completion. Anti-patterns enforced
  in the Job manifest itself: `securityContext.privileged: false`,
  `hostNetwork: false`, `hostPID: false`, `hostIPC: false`, `backoffLimit: 0`
  (one shot, no retries). RBAC required: `create`/`get`/`watch`/`delete`
  on `jobs`+`pods`, `get` on `pods/log`+`pods/exec`. No cluster-admin.
- **`slurm`** (v0.6.1) — runs the workload as a SLURM batch job via
  `sbatch` + `sacct` shell-out. Required options: `partition`. Optional:
  `account`, `qos`, `time`, `nodes`, `gres`, `mem`, `constraint`. The
  capsule directory MUST be on a filesystem shared between submit and
  compute nodes (typically `/scratch`, `/lustre/...`, or the user's home).
  The runner does not rsync artifacts; it trusts the shared FS.

Patches are removed after the run. If an SDK is not installed, its hook is
silently skipped. Capture works even if none of the AI SDKs are present — the
capsule is still written with environment, stdout/stderr, and timing.

**For uninstrumented agents** (Claude Desktop, Cursor, third-party SDKs that do not import the Python `mcp` package), `nova mcp-proxy` is a transparent stdio proxy that records the same `tool-calls.jsonl` schema by sitting between the client and an upstream MCP server. See `docs/cli-reference.md` and ADR-0015 §Secondary; experimental in v0.5.x.

---

## model-calls.jsonl

Every LLM API call intercepted by the capture hooks is recorded as one JSONL
line following the [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

```json
{
  "schema_version": "0.1.0",
  "semconv_version": "1.30.0",
  "model_call_id": "01HXAY7M6FN9TQGE0V0M7PAY1Q",
  "parent_span_id": "657bff2c61ddad1c",
  "started_at": "2026-05-09T12:34:56.123456Z",
  "finished_at": "2026-05-09T12:34:57.353456Z",
  "duration_ms": 1230,
  "status": "success",
  "gen_ai.system": "openai",
  "gen_ai.operation.name": "chat",
  "gen_ai.request.model": "gpt-4o",
  "gen_ai.response.model": "gpt-4o-2024-08-06",
  "gen_ai.response.id": "chatcmpl-abc123",
  "gen_ai.request.temperature": 0.7,
  "gen_ai.request.max_tokens": 1024,
  "gen_ai.request.top_p": 0.95,
  "gen_ai.request.seed": 42,
  "gen_ai.request.stop_sequences": ["END"],
  "gen_ai.request.messages": [...],
  "gen_ai.response.choices": [...],
  "gen_ai.response.finish_reasons": ["stop"],
  "gen_ai.usage.input_tokens": 42,
  "gen_ai.usage.output_tokens": 18,
  "endpoint": "https://api.openai.com/v1/chat/completions"
}
```

The full set of OTel "Required when applicable" fields is extracted by both
the per-SDK hooks (`_openai`, `_anthropic`) and the wire-level hooks
(`_httpx`, `_requests`, `_aiohttp`, `_urllib3`) when present in the request
body — so `nova replay --mode exact` has the determinism inputs it needs
(temperature, top_p, seed) regardless of which transport the captured call
took. See [`design/spec/model-call-v0.md`](../design/spec/model-call-v0.md) for the full
field reference.

This format is stable across providers and is the basis for `nova diff` call
alignment and `nova replay` mock serving.

---

## Environment Lock (`env.lock`)

The environment lock records the full execution environment at capture time:

- Python version and interpreter path
- All installed packages (up to 200)
- Safe environment variables (secrets are excluded)
- Host OS, CPU architecture, CPU count, total memory
- GPU presence
- Detected package manager (uv.lock, poetry.lock, requirements.txt, etc.)

`nova replay` uses `env.lock` to warn about environment mismatches before
re-executing the command.

---

## Secret Scanning and Redaction

Before a capsule is finalized, `SecretScannerV0` scans all JSONL artifacts
for 12 LLM provider key patterns (Anthropic, OpenAI, HuggingFace, Replicate,
Langfuse, and others). Detected values are redacted in-place as
`[REDACTED:rule-id]`.

After scanning, `redaction-proof.json` is written. It records:

- How many findings were detected
- Which files were scanned
- A cryptographic chain hash proving the scan ran

Capsules without `redaction-proof.json` are considered invalid by
`nova validate`.

---

## Replay Modes

A replay re-executes a capsule with all external calls controlled by
NovaFabric.

### forensic mode

Read-only inspection. NovaFabric does not spawn a subprocess. It reads and
returns the manifest, traces, and model calls directly from the capsule.
No network access, no mutation.

Use forensic mode to inspect what happened without any risk of side effects.

### mocked mode

The original command is re-spawned as a subprocess. All LLM calls are
intercepted and served from the capsule cache in order
(`MockModelDispatcher`). Tool calls are mocked or denied according to the
**safety ladder**.

### Safety ladder (mocked replay)

Mocked replay gates tool call re-execution behind explicit flags:

```
(none)              — deny all tool calls
  ↓  --allow-readonly
read-only           — permit read-only tool calls
  ↓  --allow-mutating
idempotent-write    — permit idempotent writes
non-idempotent-write
  ↓  --allow-external-side-effects
external-side-effect — permit calls with external effects
  ↓  --allow-unknown-mutation
unknown             — permit unclassified tool calls
```

Per-tool overrides can be specified in `replay.yaml`.

---

## Structural Diff

`nova diff` compares two capsules field by field:

- **Model call alignment** — calls are matched by `span_id`, then by
  position + prompt hash. Aligned pairs are compared for model, token counts,
  and response content.
- **Tool call alignment** — calls are matched by `tool_name` + argument hash.
- **Environment diff** — Python version, OS, architecture, interpreter path.
- **Output diff** — stdout/stderr by content hash.

`DiffReport` has three counters: `changed`, `added`, `removed`. The
`--assert-no-regressions` flag exits 1 if any of these are non-zero, making
it suitable as a CI gate.

---

## Lineage Graph

The lineage graph is a directed graph stored in SQLite with two table types:

- **`lineage_nodes`** — runs, assets, and artifacts. Each node has a
  deterministic ID (SHA-256 of `kind:ref`).
- **`lineage_edges`** — typed, directional edges between nodes.

### Edge types

| Edge type | Meaning |
|---|---|
| `consumed` | The run read/used this asset or artifact |
| `produced_by` | This artifact was produced by the run |
| `replayed_from` | This run is a replay of the referenced run |

### Confidence levels

| Confidence | Meaning |
|---|---|
| `observed` | Directly recorded at runtime (e.g., explicit API call) |
| `inferred` | Derived from capsule structure (e.g., output file presence) |

### Graph queries

| Query | Direction | Use case |
|---|---|---|
| `provenance` | Backward (ancestors) | "What did this run depend on?" |
| `blast-radius` | Forward (descendants) | "What runs consume this asset?" |
| `replay-chain` | Backward via `replayed_from` | "What is the original run for this replay?" |
| `time-travel` | Backward, filtered by timestamp | "What was the lineage state as of time T?" |

---

## OpenLineage Integration

NovaFabric emits [OpenLineage](https://openlineage.io) 2.0.2 events from
capsules. Events are typed as `START` and `COMPLETE` (or `FAIL`).

Events can be emitted:
- Automatically at capture time if `OPENLINEAGE_URL` is set
- On demand via `nova lineage emit-openlineage`

This enables integration with data catalog tools such as Marquez, Atlan, and
OpenMetadata.

---

## AI Asset

An **AI asset** is any versioned artifact in an ML or LLM system. NovaFabric
tracks seven types:

| Type | What it represents |
|---|---|
| `model` | A trained ML/LLM model — framework, artifact path |
| `agent` | An AI agent — model ref, tools, prompts, policies, eval suites |
| `prompt` | A prompt template used by one or more agents |
| `tool` | A callable function exposed to an agent |
| `dataset` | A dataset used for training, fine-tuning, or evaluation |
| `evaluation` | An evaluation suite definition (test cases, scoring logic) |
| `deployment` | A running endpoint — base URL, environment |

Assets are described by YAML spec files and stored in the local asset registry.
Every registered asset is pinned to a git commit SHA and carries a lifecycle
status that tracks where it is in your development-to-production pipeline.

---

## Asset Lifecycle

### The state machine

```
development ──► validated ──► pending_approval ──► staging ──► production ──► archived
      │                                                  ▲
      └──────────────────────────────────────────────────┘
           (direct skip: development → staging or production)
```

All six statuses in plain language:

| Status | Meaning |
|---|---|
| `development` | Work in progress. Default when registered. No constraints. |
| `validated` | Passed automated checks (`nova validate`). Spec is well-formed, secrets are clean. |
| `pending_approval` | Waiting for a human sign-off (`nova approve`). Useful in regulated environments. |
| `staging` | Cleared for pre-production use. **Agents require a passing eval to reach here.** |
| `production` | Live. **Agents require a passing eval to reach here.** |
| `archived` | Retired. No further promotion possible. Capsules and lineage are still readable. |

Direct skips (e.g. `development → production`) are allowed. Once archived, an
asset cannot be re-promoted. The `validated` and `pending_approval` steps are
optional — small teams often go straight from `development` to `staging`.

### What actually changes when you promote an asset

**Promoting an asset updates one database record.** That is all NovaFabric does.

```
nova promote direct my-agent@v1.2.0 --to production --actor alice
```

Internally this:
1. Checks the policy engine — allow or deny, written to the audit log.
2. For `agent` type going to `staging`/`production`: verifies a passing eval
   result exists. Blocks if not (unless `--force`).
3. Runs `UPDATE assets SET status = 'production', promoted_at = ..., promoted_by = 'alice'`.
4. Returns the updated record.

**Nothing else happens.** Your agent does not restart. Your prompt does not
reload. No traffic is rerouted. No container is restarted. The capture hooks
and runners are unchanged.

The status is **governance metadata** — a reliable signal your tooling can
read and act on. NovaFabric gives you the signal; your platform decides what
to do with it.

### How external tooling consumes the status

The pattern is: your CI/CD pipeline (or any script) reads the status from
NovaFabric and makes a deployment decision based on it.

```bash
# In a GitHub Actions deploy step or Argo CD sync hook:
STATUS=$(nova inspect my-agent@v1.2.0 | jq -r .status)

if [ "$STATUS" = "production" ]; then
  kubectl set image deployment/my-agent container=my-agent:v1.2.0
else
  echo "Asset not in production — deploy blocked (status=$STATUS)"
  exit 1
fi
```

NovaFabric doesn't own your deployment. It owns the evidence that the asset
is ready for deployment.

### Use cases

**Use case 1 — CI/CD gate for an LLM-powered feature**

You have a customer-facing summarisation agent. Your CI pipeline runs evals
on every PR. Only if evals pass and a team lead approves does the agent reach
`production`, which triggers your Helm chart to update the image tag.

```bash
# .github/workflows/deploy-agent.yml (simplified)
- run: nova eval summariser@${{ github.sha }} --suite regression
- run: nova promote direct summariser@${{ github.sha }} --to pending_approval --actor ci-bot
# ... human approves in dashboard or via CLI ...
- run: |
    nova promote direct summariser@${{ github.sha }} --to production --actor ${{ github.actor }}
    helm upgrade summariser ./chart --set image.tag=${{ github.sha }}
```

**Use case 2 — Safe prompt rollout without a full redeploy**

You update a system prompt template. You don't want to redeploy the entire
agent service — just swap the prompt version your agent reads at startup. The
registry tells your agent loader which version is `production`.

```python
# agent startup code
from novafabric.registry.service import get_asset

asset = get_asset("system-prompt-v2", version="latest")
if asset["status"] != "production":
    raise RuntimeError(f"Prompt not promoted to production (status={asset['status']})")

prompt_text = json.loads(asset["spec_json"])["spec"]["template"]
```

If the prompt has a bug and you archive it, the next agent restart fails fast
rather than silently loading a bad prompt.

**Use case 3 — Audit trail for a regulated environment**

Your company needs to demonstrate that no AI model reached production without
a human sign-off. Every promotion through `pending_approval` creates an
approval record (approver, timestamp, note) and an audit log entry. At audit
time:

```bash
nova inspect risk-scorer@v3.1.0 --format json | jq '{
  status,
  promoted_by,
  promoted_at,
  forced_promotion
}'
# → { "status": "production", "promoted_by": "alice", "promoted_at": "2026-03-15T10:22:01Z", "forced_promotion": false }
```

No manual tracking spreadsheet. The registry is the record.

**Use case 4 — Blast-radius control when an agent misbehaves**

An agent in production starts producing bad outputs after an upstream model
update. You archive it:

```bash
nova promote direct my-agent@v2.0.0 --to archived --actor on-call-eng
```

Your deployment hook detects the status change, rolls back to the previous
`production` version (`my-agent@v1.9.0`), and pages the team. The capsules
from the bad version are still fully intact for forensic replay — nothing
is deleted.

**Use case 5 — Multi-team handoff on an HPC cluster**

A research team develops and validates an agent on their workstation. When
they're ready for the ML engineering team to integrate it into the batch
pipeline, they promote it to `staging`. The ML team's Slurm job template only
launches agents whose status is `staging` or `production`:

```bash
# check_asset_status.sh — called from sbatch prolog
STATUS=$(NOVAFABRIC_DB_PATH=/shared/nova/registry.db nova inspect $AGENT_NAME@$AGENT_VERSION | jq -r .status)
if [[ "$STATUS" != "staging" && "$STATUS" != "production" ]]; then
  echo "PROLOG FAIL: $AGENT_NAME@$AGENT_VERSION is not ready (status=$STATUS)" >&2
  exit 1
fi
```

The registry on the shared filesystem (`/shared/nova/registry.db`) is the
handoff protocol between the two teams — no Slack messages, no wiki pages.

---

## Asset Registry

The registry is a local SQLite database at `~/.novafabric/registry.db`
(override: `NOVAFABRIC_DB_PATH`). It stores:

| Table | Contents |
|---|---|
| `assets` | Every registered asset — name, version, type, status, full spec JSON, git SHA, promotion history |
| `eval_results` | Pass/fail eval results per asset, per suite |
| `approvals` | Human sign-off records: approver, timestamp, note |

The registry is entirely local. There is no server, no credentials, and no
network dependency for local mode.

In server mode (`nova serve`), the same data lives in Postgres and is
accessible via the REST API at `/api/v1/assets`.

---

## Eval-Gated Promotion

`nova eval <agent@version>` discovers evaluation suites declared in the
agent spec (`spec.evals`) and resolves them via Python entry points in the
`novafabric.evals` group. Results are stored in the `eval_results` table.

Promotion to `staging` or `production` queries `eval_results` for a passing
result. If none exists, promotion is blocked unless `--force` is used. A
forced promotion is recorded as such (`forced_promotion = true`) and appears
in `nova inspect` output and the audit log — the escape hatch is visible,
not silent.

---

## NovaSeal — Capsule Signing (v0.10)

**experimental** (v0.10+) When `~/.novafabric/novaseal.yaml` (or `NOVAFABRIC_SEAL_CONFIG`)
is present, `nova capture` automatically signs the capsule after all artifacts are
written. Signing never blocks or fails the capture — if the key is unavailable, a
warning is printed and the unsigned capsule is kept.

A sealed capsule adds a `.seal/` directory:

```
.seal/
  manifest.dsse       ← DSSE envelope: base64url(manifest_json) + ECDSA P-256 signature + embedded cert
  manifest.dsse.tsr   ← RFC 3161 TSR from the configured TSA (DER binary)
  log-entry.json      ← Merkle log entry: leaf_index, leaf_hash, root_hash, tree_size
```

The signing flow:

1. **Payload** — `capsule.yaml` is JSON-serialised (deterministic, sorted keys).
2. **DSSE envelope** — PAE (Pre-Authentication Encoding) is signed with ECDSA P-256; the cert is embedded so the envelope is self-contained.
3. **Timestamp** — SHA-256 of the DSSE bytes is sent to the configured TSA; the TSR is stored raw.
4. **Merkle log** — a leaf is appended to the tenant's SQLite Merkle tree; the inclusion proof is stored in `log-entry.json`.

Verify with:

```bash
nova verify .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
# signature_ok=True, timestamp_ok=True, log_integrity_ok=True
```

**v0.1 scope:** local ECDSA P-256 key only. Sigstore keyless and cloud KMS are **planned** for v0.2. See [ADR-0041](../design/adr/0041-novaseal-cryptographic-core-adoption.md).

---

## SDK Decorator

`@novafabric.agent` is an in-process alternative to `nova capture`. It wraps
an agent function with the same capture hooks used by the CLI, recording all
LLM calls into a capsule. Without `capsule_dir`, it emits OTel spans only —
this is the v0.1 observability mode.

---

## ULID

Run IDs and replay IDs are [Universally Unique Lexicographically Sortable
Identifiers](https://github.com/ulid/spec). They embed a millisecond timestamp
as the high bits, so capsule directories naturally sort chronologically without
a separate index.
