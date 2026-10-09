# Run Capsule anatomy

[Architecture, as built](README.md) › Run Capsule

A Run Capsule is a plain directory named by a ULID. It holds every observable
fact about one execution. You can copy it, archive it, attach it to a ticket or
verify it without a server, an account or a database.

**Maturity:** works today. The on-disk format is validated against
`schemas/run-capsule.schema.json`. It is not frozen until the v1.0 schema
freeze, but changes are additive and optional, so older capsules stay valid.

![How a Run Capsule is assembled, file by file](../assets/architecture/capsule-assembly.svg)

## Where capsules live

| Setting | Default | Resolved by |
|---|---|---|
| `NOVAFABRIC_CAPSULE_DIR` | — | `_paths.py:default_capsule_dir` |
| `NOVAFABRIC_HOME` | `~/.novafabric` | `_paths.py:nova_home` |
| Effective capsule root | `$NOVAFABRIC_HOME/capsules/` | |
| Per-run override | `nova capture -o <dir>` | `cli/capture.py` |

The run ID is a 26-character [ULID](https://github.com/ulid/spec)
(`capture/_ulid.py`; schema `$defs/Ulid`). ULIDs sort by creation time, so a
directory listing is already in chronological order.

## The files, in write order

`CaptureOrchestrator.run` in `capture/orchestrator.py` writes the files in this
order. A failed run gets the same set of files.

| # | Path | Holds | Written by |
|---|---|---|---|
| 1 | `inputs/`, `outputs/` | Inputs, and the workload's outputs | `capture/capsule.py:CapsuleWriter.open` |
| 1 | `model-calls.jsonl` | LLM call records, keyed with OTel GenAI attributes (`gen_ai.request.*`, `gen_ai.response.*`); a call made through the OpenAI/Anthropic SDK also leaves one `transport` record per HTTP attempt (see [Model-call record roles](#model-call-record-roles)) | hooks, through `CapsuleWriter` |
| 1 | `tool-calls.jsonl` | One record per tool invocation | hooks, through `CapsuleWriter` |
| 1 | `trace.jsonl` | Execution spans, including the root span | hooks and orchestrator |
| 1 | `assets.jsonl` | References to registry assets the run consumed | hooks, through `CapsuleWriter` |
| 2 | `outputs/stdout.txt`, `outputs/stderr.txt` | Process output | orchestrator |
| 2 | `outputs/<sha>.<ext>` | Media blobs, content-addressed | `capture/media.py` |
| 3 | `env.lock` | Environment snapshot: `mode` (`deterministic` or `best-effort`), host, Python interpreter, installed packages | `capture/env.py:capture_environment` |
| 4 | `replay.yaml` | Replay policy (`schemas/replay-policy.schema.json`); capture writes a minimal policy, which you can extend | `capture/replay.py:minimal_replay_policy` |
| 5 | `capsule.yaml` | The run manifest, redacted as a data structure before it is written | orchestrator |
| 6 | `lineage.jsonl` | Typed lineage edges for this run | `lineage/_writer.py:LineageWriter` |
| 7 | `capture-health.json` | Dropped-event counts, only when the recorder dropped events. Written before the residual pass, so it is scanned and listed in `evidence_digests` | `capture/event_recorder.py:EventRecorder.finalize_health` |
| 8 | `redaction-proof.json` | The secret-scan proof (see below), written once after the residual pass | `capture/secrets.py:SecretScannerV0` |
| 9 | `capsule.yaml` (rewritten) | The manifest again, now with `evidence_digests` | orchestrator, `_evidence_digests` |
| 10 | `.seal/manifest.dsse`, `.seal/manifest.dsse.tsr`, `.seal/log-entry.json` | The seal, only when NovaSeal is configured | orchestrator, `_seal_capsule` |

These files appear only in some runs:

| Path | When |
|---|---|
| `network_events.jsonl`, `file_events.jsonl`, `human_approvals.jsonl` | When the event stream has at least one record (`capture/event_recorder.py`) |
| `capture-health.json` | When the recorder had to drop events. Its absence means nothing was dropped up to the residual pass. Written before that pass (unreleased, on `main`; v0.104.0 writes it after sealing, unbound), so it is scanned and listed in `evidence_digests`; an event dropped after the digest map was computed cannot be added without breaking the seal, so capture logs a warning for it instead. |
| `c2pa-manifest.json` | `nova capture --mark-provenance` |
| `otel-genai-spans.json` | `nova capture --emit-otel-genai` |

```mermaid
flowchart TB
    root["01J…ULID/"]
    root --> m["capsule.yaml<br/><i>manifest + evidence_digests</i>"]
    root --> s1["model-calls.jsonl"]
    root --> s2["tool-calls.jsonl"]
    root --> s3["trace.jsonl"]
    root --> s4["assets.jsonl"]
    root --> e["env.lock"]
    root --> r["redaction-proof.json"]
    root --> rp["replay.yaml"]
    root --> l["lineage.jsonl"]
    root --> io["inputs/ · outputs/"]
    root --> seal[".seal/ (optional)"]
    seal --> d["manifest.dsse"]
    seal --> t["manifest.dsse.tsr"]
    seal --> le["log-entry.json"]
    m -. "sha256 of every file except<br/>capsule.yaml and .seal/" .-> s1 & s2 & s3 & s4 & e & r & rp & l & io
    d -. "signs" .-> m
```

## The manifest: `capsule.yaml`

The schema requires `schema_version`, `run_id`, `created_at`, `finished_at`,
`duration_ms`, `status`, `command`, `capture_mode`, `novafabric_version`,
`working_directory`, `host`, the `*_ref` pointers to the files above,
`trace_root_span_id`, `inputs`, `outputs`, and the call counters
(`model_call_count`, `tool_call_count`, `mutating_tool_count`). `model_call_count`
counts logical model calls, not records (see below).

### The `host` block

**Works today on `main` (unreleased; v0.104.0 adapter and decorator capsules still carry
the hardcoded values below).** `capsule.yaml:host` is built by one function, `capture/env.py:host_info`, for every
capsule writer: `nova capture` (`CaptureOrchestrator`), the framework adapters
(`adapters/_capsule.py`) and the `@agent` decorator (`novafabric.sdk.agent`, in `sdk/agent.py`).
Before this was unified, adapter and decorator capsules wrote `arch: x86_64`,
`cpu_count: 1` and `memory_bytes: 0` whatever the machine;
`tests/capture/test_host_arch_is_never_hardcoded.py` now fails on any literal for
a measured host field.

| Field | Source | Limits today |
|---|---|---|
| `arch` | `capture/env.py:host_arch`, which `env.lock` and the OTLP importer also use: `platform.machine()` normalised (`aarch64` → `arm64`, `amd64` → `x86_64`) | an unmapped machine string is recorded as reported, never guessed |
| `os` | `platform.system()`, lower-cased | the schema allows only `linux`, `darwin` and `windows`; any other OS is recorded as `linux` |
| `cpu_count` | `os.cpu_count()` | `1` if it cannot be read |
| `memory_bytes` | `MemTotal` from `/proc/meminfo` | `0` where there is no `/proc/meminfo` (macOS, Windows): read `0` as "not measured" |
| `python` | `platform.python_version()` | |
| `gpu` | — | always `[]` today: no GPU inventory is collected (`env.lock:hardware.gpus` is empty too) |
| `hostname_redacted` | — | always `true`; the hostname is never written to the manifest (`env.lock` keeps a SHA-256 of it) |

## Model-call record roles

**Works today** (ADR-0305, unreleased). A call made through the OpenAI or Anthropic SDK
is recorded by two hooks: the SDK hook writes the **logical** record (the parsed response,
`io.novafabric.api_surface`), and the wire hook (`httpx`) writes one **transport** record
per HTTP attempt underneath it, with no response. Both are kept: the transport records are
the only evidence of SDK-internal retries (a 429 followed by a 200) and of what went over
the wire. Two optional `extensions` keys say which is which:

| Key | Value |
|---|---|
| `io.novafabric.record_role` | `logical` or `transport` |
| `io.novafabric.logical_call_id` | The `model_call_id` of the logical call: its own id on a logical record, the covering SDK record's id on a transport record (every retry of one SDK call links to the same id) |

A wire record with no SDK call around it (raw `httpx`, a non-SDK client) is `logical`.
An unmarked record is logical (adapter, API-proxy and OTel-ingest records carry no marker).

Everything that counts or iterates model calls reads logical records only:
`model_call_count`, `nova cost`, `nova diff`, mocked replay and its counters, `nova query`,
the dashboard (`/api/runs/{id}` returns transport records separately as
`transport_model_calls`), the knowledge graph, and the exporters. Evidence surfaces that
hash, scan or bundle the file read every record. A transport record whose logical record
is missing (the call was cancelled before the SDK hook wrote it) counts once, so a call
is never dropped from a count. The rule lives in one place,
`novafabric.capture.record_roles` (`logical_model_calls`, `is_transport_record`).

**Capsules captured before ADR-0305** carry no marker. Readers recognise the old
duplicate shape: a response-less wire record followed by an SDK record for the same
request (model + messages) that encloses it in time, or, without timestamps, follows it
with only retries in between. Anything less certain is counted, so the fallback never
under-counts. The `model_call_count` sealed into an old `capsule.yaml` is not rewritten:
it keeps the doubled value it was signed with.

Some optional fields matter for the architecture:

| Field | Purpose |
|---|---|
| `evidence_digests` | `{path: {sha256, size_bytes}}` for every file except `capsule.yaml` and `.seal/`. This field is what binds the whole directory to the seal. |
| `parent_run_id`, `replay_of_run_id`, `replay_mode` | Relationships to other capsules. `replay_of_run_id` becomes a `replayed_from` lineage edge. |
| `session_id`, `sequence` | Membership in a multi-turn session (experimental) |
| `facets`, `extensions` | Additive extension points (`slurm`, `kubernetes`, …) that let the format grow without a new top-level format |
| `exit_code`, `error` | The failure record. A failed run is still a complete capsule. `error.type` is `NonZeroExit` when the workload ran and exited non-zero, and `WorkloadNotStarted` when the runner could not start it (`runner_status: failed_setup`, e.g. `command not found: <argv0>` from `runners/_local.py`), so the record never claims that something exited (`WorkloadNotStarted` is unreleased, on `main`). |

## The redaction proof: `redaction-proof.json`

The secret scanner runs on every capture. It scans and redacts, in place, the
call and event streams (`capture/secrets.py:SCAN_TARGETS`), `env.lock`,
`assets.jsonl`, `lineage.jsonl` and every file under `inputs/` and `outputs/`
(`ARTIFACT_SCAN_TARGETS`, `ARTIFACT_SCAN_DIRS`). The manifest is redacted before
`capsule.yaml` is written. Binaries are scanned by string extraction, and a binary
that carries a key-shaped match is dropped. A final residual pass rescans every
file in the finished capsule before `evidence_digests` is computed, and the final
manifest is checked again before it is sealed. The rule pack is `gitleaks-core-v0`
0.7.0, which matches known key formats only. Its proof file records:

- the SHA-256 of each scanned file before redaction and as finally written;
- for each finding: the rule ID, byte offset, length, redaction strategy, and the
  SHA-256 of the matched secret (never the secret itself);
- the hash of the rule pack, and a `chain_hash` over the canonical proof;
- `masker_findings[]` and `masker_errors[]` from the pluggable masking pipeline
  (`masking/`);
- `residual_check`: what the final pass rescanned, any residual it redacted, and
  any file whose after-hash it updated because the file changed after its first
  scan.

`redaction-proof.json` is listed in `evidence_digests`, so a seal covers the proof
along with everything else.

### What the scanner detects, and what it does not

The proof records what was scanned and what was redacted. It is **not** proof that
a capsule contains no secret: detection is by pattern, so a secret in a format no
rule knows passes through unchanged and the proof shows zero findings for it.

**Detected** by `gitleaks-core-v0` 0.7.0 (18 rules, `capture/secrets.py:_RULES`):
API keys with a known prefix for OpenAI, Anthropic, Hugging Face, Replicate,
LangFuse, LangSmith, Weaviate, Qdrant and Pinecone (`pckey_`/`pcsk_`); Cohere,
Together and Mistral keys by shape alone (a bare 40-character alphanumeric, 64-hex
or 32-character alphanumeric token), which is also why those three rules never
cause a binary file to be dropped; AWS access key IDs (`AKIA`, `ASIA`, `ABIA`, `ACCA`, `A3T…`); an AWS secret access
key *when its key name is next to it* (`AWS_SECRET_ACCESS_KEY=…`,
`"SecretAccessKey": "…"`); GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
`github_pat_`); NovaFabric's own API keys and webhook secrets.

**Not detected** by the default pack (checked against pack 0.7.0):

- a bare 40-character AWS secret access key with no key name next to it (one
  made only of letters and digits may still be caught, by accident, by the
  Cohere shape rule; one containing `/` or `+` is not);
- a legacy Pinecone key that is a bare UUID (indistinguishable from a run ID);
- private keys in PEM form (`-----BEGIN … PRIVATE KEY-----`);
- JWTs and generic `Authorization: Bearer …` tokens;
- passwords, including `password=…` assignments and credentials inside a
  connection string such as `postgres://user:pass@host/db`;
- Slack, Stripe, Google Cloud / Google API, and Azure keys, and any other
  provider whose prefix is not in the list above;
- generic high-entropy strings (there is no entropy rule);
- personal data such as email addresses, names or phone numbers. The opt-in
  `novafabric-email` masker (ADR-0135, experimental) masks email addresses when
  you configure it; nothing masks other personal data by default.

Treat a capsule as **secret-scanned**, not as secret-free. If your workload can
print credentials in a format above, add a masker for it or keep it out of the
captured output.

## Capsules written inside a framework call

**Works today** (the streaming behaviour, the measured `host` block, the replay
refusal and the shared finalization described here are unreleased, on `main`). The framework
adapters (`src/novafabric/adapters/*.py`; LlamaIndex, Pydantic AI and Haystack share
`adapters/_capsule.py:AdapterCapture`, the others write their own manifest) and the
`@agent` decorator (`novafabric.sdk.agent`, in `sdk/agent.py`) write a capsule from inside the
Python process:

- `capture_mode: sdk-decorator`, and `command` is a label such as
  `@langgraph:demo`, not a program. `nova replay --mode mocked` refuses such a
  capsule up front (see [Replay modes](replay-modes.md#which-capsules-each-mode-accepts)).
- `host` comes from `host_info()` and `model_call_count` counts logical calls, as
  above.
- A streamed run in the LangGraph, LlamaIndex or Pydantic AI adapter is captured
  until the stream ends (`adapters/_streaming.py`). One the caller closed early, dropped unread or
  cancelled is `status: partial` with `metadata.partial_reason` (`abandoned` or
  `cancelled`); one that raised mid-stream is `failure`, and the exception still
  propagates. In these adapters and Haystack, a wrapped call made from inside a run
  the same adapter is already capturing records into the open capsule instead of
  opening a second one.
- Finalization is the same as for `nova capture` (unreleased, on `main`): every
  writer calls `capture/finalize.py:finalize_in_process_capsule` after writing
  `env.lock` and `replay.yaml`. The secret scanner runs over the streams, `env.lock`
  and `inputs/`/`outputs/`; the manifest is redacted before it is written;
  `lineage.jsonl` is written and indexed; the residual pass rescans every file
  (including `replay.yaml` and anything written late); `redaction-proof.json` is
  written once; `evidence_digests` binds every file; the final manifest is gated;
  and the capsule is **sealed when a signing profile exists** (opt-in, as for
  `nova capture`), so `nova verify` checks it the same way. Configured maskers
  (ADR-0135) are a `nova capture` option and do not run here.
- Finalization never fails the wrapped call. If it raises, if the manifest gate
  finds a secret, or if a configured seal fails, the capsule is left unsealed (any
  partial `.seal/` is removed), the reason is written to
  `metadata.finalization_error` (redacted), and a warning is logged; the call's
  own result or exception is unchanged.

OTLP-ingested capsules (`capture_mode: otel-import`, `metadata.capture_level:
ingested-otlp`, written by `otel/genai_ingest.py` for `POST /api/otlp/v1/traces`)
finalize through the same `finalize_in_process_capsule` call (unreleased, on
`main`): main scan, manifest redaction (an agent name from a span lands in the
manifest), residual pass, `evidence_digests`, gate, and a seal when a signing profile
exists. A failure keeps every ingested record and leaves the capsule unsealed with
`metadata.finalization_error`. The `ingested-otlp` label is permanent; a seal does not
upgrade it. See [OTLP ingest](otlp-ingest.md).

## Parent and child capsules (prototype)

Distributed runs, such as a SLURM job across several nodes, use a separate
writer, `capsule/writer.py:CapsuleWriter` with `ParentCapsuleTracker`. It records
one PARENT plus N WORKER capsules under a shared `global_run_id`, in a
`capsule.json` that follows `schemas/parent_child_capsule_v1.schema.json`.
`capsule/orphan.py` handles workers whose parent never finalised. The commands
are `nova run new-run-id | validate-distributed | show | lineage`
(`capsule/cli.py`).

> Two classes named `CapsuleWriter` exist: `capture/capsule.py` writes ordinary
> run capsules, and `capsule/writer.py` writes the distributed parent/child
> layout.

## Read next

- [Sealing and verification](sealing-and-verification.md): how `evidence_digests`
  becomes a signature.
- [Concepts › Run Capsule](../concepts.md#run-capsule)
