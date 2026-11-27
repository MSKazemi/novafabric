---
name: novafabric-instrument
description: Use when a user wants to add NovaFabric to a Python AI agent — capture its runs as Run Capsules (replayable folders you own, sealable with your own key), validate them, verify the seal, replay or export evidence. Triggers — "instrument my agent with NovaFabric", "capture my agent runs", "add NovaFabric", "make my agent replayable/auditable", "record my LLM calls", "I want evidence of what my agent did". Python workloads; local, no server needed for capture.
---

# Instrument a Python AI agent with NovaFabric

NovaFabric records one run of a command as a **Run Capsule**: a folder you own with the
run's model calls, tool calls, trace, environment lock and outputs. Capture, validate,
verify and replay run locally with no server. A capsule is **unsealed** unless the user
has turned sealing on, and by default it **contains prompt and response text** (see
"Honest limitations"). Say both before the first capture.

## When to use / not use
- **Use** for a Python agent or LLM app whose runs the user wants to record, replay,
  verify or turn into evidence.
- **Not for non-Python clients** without reading `nova api-proxy --help` and
  `nova mcp-proxy --help` first: automatic model-call capture runs inside a Python
  interpreter; those two proxies record HTTP model APIs and MCP servers for other clients.
- **Not for deploying a server or dashboard** — that is the `novafabric-deploy` skill.

## Procedure

Run each step, read its output, and confirm it before moving on.

1. **Find the entrypoint and its Python environment.** Identify how the agent runs
   (`python agent.py`, `python -m myagent`, `uv run …`) and which interpreter it uses
   (an activated venv, `.venv/bin/python`, `uv run`). No code change to the agent is needed.

2. **Install NovaFabric into that same environment.** The capture hooks load inside the
   agent's own interpreter (a `sitecustomize` loader that imports `novafabric`), so
   `novafabric` must be importable *there*, not only on the shell's `PATH`:
   ```bash
   pip install novafabric          # inside the agent's venv; or: uv add novafabric
   ```
   The optional local dashboard needs the extra: `pip install 'novafabric[serve]'`.

3. **First-run setup** (idempotent):
   ```bash
   nova init
   ```
   Creates `~/.novafabric/` (or `$NOVAFABRIC_HOME`) with `capsules/`, `keys/`,
   `replays/` and an Ed25519 key pair (mode 600) used to sign **Evidence Bundles**. It does
   **not** turn on capsule sealing; its last line says
   "Capsule sealing is off until you run `nova seal init`".

4. **Optional — turn on sealing** (experimental), only if the user wants tamper-evident
   capsules:
   ```bash
   nova seal init
   ```
   Fully offline. It creates a local ECDSA P-256 signing key and a local seal CA. The
   identity is **self-asserted**: a seal proves that the holder of this key signed, not
   who that holder is. No RFC 3161 timestamp is configured, so seals carry no trusted time.
   Only captures made *after* this step are sealed. Details:
   [NovaSeal configuration](https://github.com/MSKazemi/novafabric/blob/main/docs/novaseal-configuration.md).

5. **Capture a run.** Wrap the existing command, using the environment from step 1:
   ```bash
   nova capture -- python agent.py [args...]      # or: uv run nova capture -- python agent.py
   ```
   The `--` stops `nova` from reading the agent's own flags. The output prints
   `Capsule written: <path>  (run_id=<run-id>)`; by default the path is
   `~/.novafabric/capsules/<run-id>/` (`--output-dir` changes the base directory). If the
   output says `Workload never started: command not found: python`, use the interpreter
   that exists (`python3`, `.venv/bin/python`).

6. **Validate, then verify:**
   ```bash
   nova validate ~/.novafabric/capsules/<run-id>/
   nova verify   ~/.novafabric/capsules/<run-id>/
   ```
   - `validate` prints `Valid capsule: <run-id>  status=success` for a well-formed capsule.
   - `verify` on an **unsealed** capsule exits **1** with `No .seal/ directory found` and
     says sealing is opt-in. That is the expected result without step 4. Tell the user so;
     do not report it as a passing check, and do not hide it.
   - `verify` on a sealed capsule ends with a summary line such as
     `signature_ok=True, … identity_trust=self-asserted`. Report the trust level too.

7. **Use the capture** (offer these, do not force them):
   - Inspect without running anything: `nova replay <capsule-dir> --mode forensic`.
     `nova replay` with no `--mode` defaults to `mocked`, which **re-runs the agent** (see below).
   - Signed Evidence Bundle (uses the key from step 3):
     `nova export-evidence <capsule-dir> --key ~/.novafabric/keys/signing_key.pem --output evidence.zip`
   - Lineage: `nova lineage provenance <run-id>`. It prints `No results` when the run
     recorded no lineage edges, which is normal for a plain script.
   - Local dashboard (experimental, needs the `[serve]` extra):
     `nova serve --experimental`. It binds `127.0.0.1:4321` and is **not read-only**: its
     admin token can delete runs and erase PII.

## Honest limitations (state these to the user)
- **Content is captured by default.** The SDK and wire model-call hooks write request
  messages and response text to `model-calls.jsonl` at every capture level.
  `NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off` drops only the generic wire hook's response
  bodies. Treat a capsule as sensitive data. The secret scanner matches known key
  formats; it cannot prove that no secret remains. `nova redact <capsule-dir>` re-scans a
  capsule for PII.
- **Unsealed by default.** Sealing starts only after `nova seal init` (or a configured
  `novaseal.yaml`), and its identity is self-asserted unless the user pins a CA.
- **Python only for automatic capture.** Hooks cover common SDKs and HTTP clients (OpenAI,
  Anthropic, Bedrock, httpx, requests, aiohttp, urllib3, MCP, Ollama). `nova api-proxy` and
  `nova mcp-proxy` cover other clients.
- **Replay has five modes** (`forensic`, `mocked`, `semantic`, `exact`, and the
  experimental `intervention`). Only `mocked` and `intervention` re-run the command:
  - `mocked` serves recorded responses for OpenAI chat completions and responses and
    Anthropic messages, and recorded results for MCP `call_tool`, functions declared with
    `novafabric.capture.record.tool`, and the supported framework tool surfaces
    (experimental). Every **other** tool (HTTP, shell, files, undeclared functions) **runs
    live**, and the network is observed, not blocked (`--hermetic` blocks it:
    experimental, Linux only).
  - `mocked` refuses up front a capsule whose model calls it cannot serve (for example
    Ollama or Hugging Face calls), unless `--permissive` is passed.
  - `mocked` needs `novafabric` importable in the agent's interpreter, as capture does.
    Otherwise the replay reports `dispatcher_install_failed`.
  - Read `nova replay --help` before promising a replay result.

## Verify it worked
`nova validate <capsule-dir>` passes, and `model-calls.jsonl` in the capsule has a record
for each model call the agent made. A passing `validate` alone does not prove the hooks
ran: check `outputs/stderr.txt` in the capsule. The line
`[novafabric] hook install failed: No module named 'novafabric'` means the agent's
interpreter cannot import `novafabric` (go back to step 2), and nothing was recorded from
inside the process. If `model-calls.jsonl` is empty with no such line, the SDK may not be on
a hooked path; read `nova capture --help` before trying anything else. If sealing was set up
first, `nova verify` reports `signature_ok=True`.
