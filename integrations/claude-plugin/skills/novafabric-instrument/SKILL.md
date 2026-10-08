---
name: novafabric-instrument
description: Use when a user wants to add NovaFabric to their AI agent — record, replay, verify, and audit agent/LLM runs as Run Capsules (sealable with your own key). Triggers — "instrument my agent with NovaFabric", "capture my agent runs", "add NovaFabric", "make my agent replayable/auditable", "record my LLM calls", "I want evidence of what my agent did". For Python agent/LLM projects; self-hosted, no server required for capture.
---

# Instrument an AI agent with NovaFabric

NovaFabric records an agent/LLM run as a **Run Capsule** — a portable, replayable
folder you own, holding the prompts, tool calls, network calls, and outputs. It is
secret-scanned against known key formats, and sealable with your own key. Once a run
is captured you can replay it, diff two runs, trace lineage, and export a signed
Evidence Bundle. It is **self-hosted**: capture, validate, replay, and lineage all
work in your own environment, offline, with no server.

## When to use / not use
- **Use** when the user has a Python agent or LLM app and wants to record / replay /
  verify / audit its runs, or wants tamper-evident evidence of agent behavior.
- **Do not use** for non-Python agents without reading `nova api-proxy --help` first
  (automatic model-call capture is Python-only; the proxy covers other clients), or for
  *deploying the dashboard/server* — that is the `novafabric-deploy` skill.

## Procedure

Work through these in order. Confirm each step's output before moving on.

1. **Confirm the project and entrypoint.** Identify the Python script or module that
   runs the agent (e.g. `main.py`, `agent.py`, `python -m myagent`). NovaFabric wraps
   this entrypoint; you do not have to change the agent's code to capture it.

2. **Install.** Prefer the user's package manager:
   ```bash
   pip install novafabric        # or: uv add novafabric
   ```
   For the optional local dashboard add the extra: `pip install 'novafabric[serve]'`.

3. **First-run setup.** Initialize the NovaFabric home (idempotent):
   ```bash
   nova init
   ```
   This creates `~/.novafabric/` (registry + an Ed25519 keypair, mode 600, used to
   sign Evidence Bundles). Docker users skip this. Captures are **not sealed** by
   default; to seal them, configure a signing profile — see
   [NovaSeal configuration](https://github.com/MSKazemi/novafabric/blob/main/docs/novaseal-configuration.md)
   (`nova seal init` does it in one command on `main`, not yet released).

4. **Capture a run.** Wrap the existing entrypoint — no code change needed:
   ```bash
   nova capture python <entrypoint> [args...]
   ```
   This produces a capsule under `~/.novafabric/capsules/<run-id>/`. For programmatic
   control instead of the CLI, use `CaptureOrchestrator` from `novafabric` around the
   agent's main call.

5. **Validate (and verify, if sealed).** Confirm the capsule is well-formed:
   ```bash
   nova validate ~/.novafabric/capsules/<run-id>/
   nova verify   ~/.novafabric/capsules/<run-id>/
   ```
   `verify` reports `signature_ok=True` only when sealing was configured before the
   capture; an unsealed capsule has no signature to check — say so, do not hide it.

6. **Use the capture (offer, don't force).** Depending on what the user wanted:
   - Replay: `nova replay ~/.novafabric/capsules/<run-id>/ --mode forensic`
   - Lineage / provenance: `nova lineage provenance <run-id>`
   - Signed evidence: `nova export-evidence ~/.novafabric/capsules/<run-id>/ --output evidence.zip --key ~/.novafabric/keys/signing_key.pem`
   - Visual dashboard (optional, experimental): `nova serve --experimental`

## Honest limitations (state these to the user)
- Automatic model-call capture is **Python-only** (wire-level hooks cover common SDKs:
  OpenAI, Anthropic, Bedrock, requests/aiohttp/urllib3, MCP); `nova api-proxy` and
  `nova mcp-proxy` cover other clients.
- **Unsealed by default** — a capture is signed only after a signing profile exists.
- **Self-hosted**: no server needed for capture/validate/replay/lineage.
- **Full capture by default** — prompts/responses are recorded; for sensitive data
  use NovaFabric's redaction/secret-scanning before exporting evidence.
- Replay fidelity depends on mode (`forensic`, `mocked`, `semantic`, `exact`, and the
  experimental `intervention`). `mocked` serves recorded model replies; tools run live.

## Verify it worked
A `~/.novafabric/capsules/<run-id>/` directory exists and `nova validate` passes; if
sealing was configured, `nova verify` reports `signature_ok=True`. If capture produced an empty capsule, the
agent's LLM SDK may not be on a hooked path — check `nova capture --help` for
supported integrations and the `[serve]`/wire-level options.
