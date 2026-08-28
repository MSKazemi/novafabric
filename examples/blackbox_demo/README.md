# BlackBox Recorder Demo

Five-minute demo of the NovaFabric capture → validate → scan-secrets → replay
→ diff → lineage → verify pipeline.

No cloud account, no live model API key, no Kubernetes cluster required.

---

## What it shows

An AI agent reviews a payment service configuration. The **bad run** recommends
disabling rate-limiting (risky). The **fixed run** recommends reducing
`max_connections` instead (safe). NovaFabric captures both runs, diffs the
behavior change, and produces a cryptographic seal.

| Capability | Command | Status |
|---|---|---|
| Capture | `nova capture` | experimental |
| Validate | `nova validate` | experimental |
| Scan secrets | `nova scan-secrets` | experimental |
| Replay forensic | `nova replay --mode forensic` | experimental |
| Diff | `nova diff` | experimental |
| Lineage provenance | `nova lineage provenance` | experimental |
| Verify seal | `nova verify` | experimental (needs NovaSeal config) |

---

## Quick start

### Prerequisites

Install NovaFabric (dev install includes `openai`):

```sh
pip install novafabric
# or in the repo:
uv sync
```

Generate a NovaSeal local key pair for `nova verify` (one-time):

```sh
mkdir -p ~/.novafabric
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
  -out ~/.novafabric/seal.key
openssl req -new -x509 -key ~/.novafabric/seal.key -days 365 \
  -out ~/.novafabric/seal.crt -subj "/CN=NovaSeal-Local"

cat > ~/.novafabric/novaseal.yaml <<'EOF'
profile: local
key_path: ~/.novafabric/seal.key
cert_path: ~/.novafabric/seal.crt
tsa_url: https://freetsa.org/tsr
merkle_db: ~/.novafabric/novaseal-merkle.db
EOF
```

> `nova verify` uses FreeTSA for RFC 3161 timestamps — requires internet.
> Set `SKIP_VERIFY=1` to skip that step in airgapped environments.

### Run the automated demo

From the repo root:

```sh
cd /path/to/novafabric
sh examples/blackbox_demo/run_demo.sh

# Airgapped (skip nova verify):
SKIP_VERIFY=1 sh examples/blackbox_demo/run_demo.sh
```

The script:
1. Starts `mock_llm_server.py` in the background (killed on exit)
2. Captures the bad run and validates the capsule
3. Scans for secrets and replays forensically
4. Captures the fixed run and diffs the two capsules
5. Queries lineage provenance
6. Verifies the cryptographic seal (unless `SKIP_VERIFY=1`)

### Manual walkthrough

Terminal 1 — start the mock server:

```sh
python examples/blackbox_demo/mock_llm_server.py
# Listening on http://127.0.0.1:9099
```

Terminal 2 — run the steps:

```sh
export OPENAI_API_KEY=sk-demo-no-key-needed
export OPENAI_BASE_URL=http://127.0.0.1:9099

nova capture -- python examples/blackbox_demo/agent.py --mode bad
BAD_RUN=.novafabric/runs/<ULID-from-output>

nova validate $BAD_RUN
nova scan-secrets $BAD_RUN
nova replay $BAD_RUN --mode forensic

nova capture -- python examples/blackbox_demo/agent.py --mode fixed
FIXED_RUN=.novafabric/runs/<ULID-from-output>

nova diff $BAD_RUN $FIXED_RUN
nova lineage provenance $(basename $BAD_RUN)
nova verify $BAD_RUN      # needs NovaSeal config + internet
```

---

## Files

| File | Purpose |
|---|---|
| `agent.py` | Demo agent — reads config, calls mock model, writes `outputs/decision.json` |
| `mock_llm_server.py` | Fake OpenAI-compatible server on `http://127.0.0.1:9099` |
| `run_demo.sh` | Automated end-to-end script (all 8 steps, exits 0) |
| `fixtures/service.yaml` | Fake service config (no real values) |
| `fixtures/prompt.txt` | System prompt for the agent |
| `fixtures/fake_api_key.txt` | Deliberately fake API key — triggers redaction scanner |
| `expected/bad_decision.json` | Expected `decision.json` for bad run |
| `expected/fixed_decision.json` | Expected `decision.json` for fixed run |

---

## Mode selection

The mock server selects the response based on the `X-Demo-Mode` request header
sent by `agent.py`:

- `X-Demo-Mode: bad` → "disable_rate_limiting" (risky)
- `X-Demo-Mode: fixed` → "reduce_max_connections" (safe)

No real LLM, no real API key, no external network call.

---

## Lineage note

`nova lineage provenance` returns results only when the consumed assets are
registered in the local registry. The demo runs do not pre-register assets, so
the provenance query returns "no results" — this is expected. To see populated
lineage, register the fixture files first:

```sh
nova register fixtures/service.yaml --name service-config --version v1
nova register fixtures/prompt.txt --name prompt-template --version v1
nova capture -- python agent.py --mode bad
nova lineage provenance $(nova list --json | jq -r '.[0].run_id')
```

---

## Related

- Demo design: `design/publications/demos/black-box-recorder-5-minute-demo.md` (private — maintainers only)
- CLI reference: `docs/cli-reference.md`
- Architecture: `design/architecture/README.md` (private; the published map is [docs/architecture.md](../../docs/architecture.md))
