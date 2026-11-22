# EvidenceLab — one deterministic incident, captured for real

An offline, zero-key incident-investigation agent run under the **actual**
`nova capture`. It makes one genuine OpenAI SDK request to a strict loopback fixture
model and two genuine read-only tool calls, so the resulting capsule holds real
`model-calls.jsonl` and `tool-calls.jsonl` records — none are injected or painted.

> **Status — D1 of the EvidenceLab plan.** Everything described here is **works today**
> except where a label says otherwise. It is one scenario (`service-incident-v1`) and
> one run. The corrected run, `nova diff`, lineage, signing and any replay claim are
> **planned** (D2) and are *not* in this directory. `record.tool` is **experimental**
> (ADR-0306 slice 1; see [replay modes](../../docs/architecture/replay-modes.md)).

## Run it

```sh
uv sync --all-extras            # or: pip install novafabric openai
python examples/evidence_lab/run.py --scenario service-incident-v1 --output-dir evidence-lab-out
```

Real output of that command (paths shortened with `…`; the run id is the real one):

```text
evidence-lab: scenario service-incident-v1 (seed=42, profile=zero-key)
run_id: 01M81HCWW2KZCQRPWWXX222DWS
capsule: …/evidence-lab-out/capsules/01M81HCWW2KZCQRPWWXX222DWS
logical model calls: 1 (expected 1) | tool calls: 2 (expected 2: read_metrics -> search_logs)
task verdict: FAIL - policy oracle rejected 'disable_rate_limiting' (forbidden_action); this is the scenario's expected unsafe outcome
harness verdict: PASS - every D1 check held (report: …/evidence-lab-out/evidence-lab-report.json)
integrity: NOT_EVALUATED (no signer configured) | replay: NOT_EVALUATED (not part of D1)
```

It takes about 12 seconds on a laptop (measured once: 12.5 s; not a benchmark). No API
key and no network beyond `127.0.0.1` (validated, not enforced; see below). The harness
writes only under `--output-dir` and a temporary directory that is removed on exit — it
does not write bytecode beside the scripts either (`sys.dont_write_bytecode`).

### Two verdicts, deliberately separate

| | Meaning | This run |
|---|---|---|
| **task verdict** | Did the *agent's decision* satisfy the pre-registered policy? Decided by a fixed policy oracle, never by the model's own explanation. | `FAIL` — the scenario is **deliberately unsafe**: the (fixture) model recommends disabling rate limiting, which the policy forbids. |
| **harness verdict** | Did every D1 observation hold: counts, order, digests, privacy, cleanup? | `PASS` |

A failing task with a passing harness is the *expected* result. Never read it as "the
agent succeeded". `integrity` and `replay` are reported `NOT_EVALUATED`: nothing was
sealed or verified, and replay was not run — a `NOT_EVALUATED` is not a pass.

## What the harness checks

Each check compares the capsule with the scenario's own fixtures, computed
independently of NovaFabric's counters and of the agent:

- exactly **one logical model call** in `model-calls.jsonl` — counted as ADR-0305 defines
  it: a record is logical unless it is explicitly marked `transport`, so an extra or
  unmarked record is a second call and fails the run. Records marked `transport` must
  belong to that call and name the same endpoint; they are reported separately. The call
  goes to exactly this run's `http://127.0.0.1:<port>/v1` with the synthetic model id
  `novafabric-mock/evidencelab-v1`, and its recorded request messages and response equal
  the golden transcript and the fixture decision (the default capture level keeps that
  text; see *Privacy*);
- exactly **two tool calls in order** — `read_metrics`, then `search_logs` — each a
  `python` transport, `read-only` mutation class, version `1`, status `success`;
- each tool record's `result_digest` equals the SHA-256 of what the fixtures say that
  tool returns;
- the strict mock accepted exactly one request and it hashes to the golden transcript
  ([`golden/model_request.json`](scenarios/service_incident_v1/golden/model_request.json));
- `nova validate` exits 0 on the capsule;
- the policy oracle, applied to the decision again by the harness, rejects the unsafe
  action.

A missing or extra record, a wrong order, a changed digest or a payload in a tool record
fails the harness with a named code. NovaFabric's own capture stays fail-open for the
workload; it is the *harness* that refuses to call a run good without the records.

## Privacy and isolation — what is and is not true

- **Tool records are digest-only** at the default (`standard`) capture level: `arguments`
  is `{}`, `result` is `null`, and the digests are what you compare. The harness never sets
  `NOVA_CAPTURE_LEVEL`, and it refuses (named `E_ENV_REJECTED`) a run whose environment
  asks for more than `standard`.
- **The model-call record is not digest-only.** At the default level NovaFabric records
  the request messages and the response in `model-calls.jsonl`; here that is the
  synthetic prompt, the fixture log lines and the fixture decision. Do not point this
  harness at real data. `capsule.yaml` also records the command line, including the
  output paths you passed.
- **Environment is built from scratch**, not filtered: the child gets only `PATH`, `LANG`,
  `LC_ALL`, `LC_CTYPE`, `TZ`, a temporary `HOME`/`XDG_*`/`NOVAFABRIC_HOME`, the base URL and
  the dummy key `sk-evidencelab-local-dummy`. Real provider keys and proxy variables are
  never forwarded. Run directly, the agent also refuses a real `OPENAI_API_KEY`, any
  other variable whose name ends in `_API_KEY` or `_API_TOKEN` (plus a short list of
  known credential names), proxy variables and a widened capture level
  (`E_CREDENTIAL_REJECTED`, `E_ENV_REJECTED`) before any SDK call. That is a name-pattern
  check, not a scan for secrets: a credential under another name (`HF_TOKEN`, …) is not
  detected, which is why the harness builds the child's environment from scratch.
- **Loopback is validated, not enforced by the OS.** `OPENAI_BASE_URL` must be
  `http://127.0.0.1:<port>/v1` (IPv4 loopback, port ≥ 1024) or the agent stops with
  `E_BASE_URL_INVALID` before building the SDK client. The tests observe the intended
  traffic; they do **not** prove hermetic network isolation, and this directory does not
  claim it. There is no outbound-connection monitor.
- Bounded: ≤ 4 steps, request body ≤ 64 KiB, model timeout and capture wall time capped
  (default 30 s and 90 s; `--timeout-seconds` must be above 0 and at most 600), no SDK retries, mock
  lifetime capped, and every child process group is killed — and the temporary directory
  removed — on success, failure, timeout, an unexpected internal error and `SIGTERM`/
  `SIGHUP`/`SIGINT` (`E_INTERRUPTED`). `SIGKILL` cannot be handled: the mock then stops
  itself after its capped lifetime, but a hung `nova capture` is only stopped by its own
  timeout.

## Layout

| File | Role |
|---|---|
| [`run.py`](run.py) | The harness: mock server + `nova capture` subprocess, isolation, evidence checks, report. |
| [`agent.py`](agent.py) | The agent: two `@record.tool` boundaries, one SDK call, the policy review. |
| [`mock_llm_server.py`](mock_llm_server.py) | Strict loopback fixture model; refuses everything but the one expected request. |
| [`evidence_scenario.py`](evidence_scenario.py) | Shared manifest loader, the tools' simulator functions, request builder, policy oracle, URL/env guards. |
| [`scenarios/service_incident_v1/`](scenarios/service_incident_v1/) | The manifest, four fixtures and the golden request transcript. |

The scenario manifest, fixtures and `evidence-lab-report.json` are **application-local
test material, not a NovaFabric format**; the only portable formats remain the Run
Capsule and the Evidence Bundle. The BlackBox demo in [`../blackbox_demo/`](../blackbox_demo/README.md)
is unchanged and still works as before.

### Fixture digests (SHA-256 of the file bytes; verify with `sha256sum`)

| File | SHA-256 |
|---|---|
| `metrics.json` | `94aabedde3e024c3ee41ecfeade52d5617e1c3c9574972e7b1c3da4d986928db` |
| `logs.jsonl` | `89488977415003c7cd497e7db93c46120e5570a4188c5197806447f1688a638c` |
| `prompt.txt` | `59940afdbe69020726f15850b9201b37be85b8b9a3b5ba61696ea374c0b00285` |
| `model_response.json` | `33f6533ee749e8944876b1e2693c91ea989b121cec1f0545d97f6b46e1c1a720` |

The canonical request body (`golden/model_request.json`, canonical JSON) hashes to
`a2d3ab9c3f24d3a9961a44ec3349a5fa3fe241073ab6fdaf955f2804efab0226`. Two runs share every
canonical hash in the report (`fixture_sha256`, `request_sha256`, `tool_sequence_sha256`,
`decision_sha256`); only run ids, timestamps and paths differ. The loader refuses a
modified fixture (`E_FIXTURE_DIGEST_MISMATCH`), so keep line endings as committed.

## Exit codes and failure names

`run.py` exits `0` (harness PASS), `2` (a named failure: `evidence_lab: FAIL code=E_…` on
stderr and in the report), or `77` (**SKIPPED**: the optional `openai` SDK is not
installed — no evidence was produced). Failures include `E_MOCK_PORT_IN_USE`,
`E_CAPTURE_TIMEOUT`, `E_CAPTURE_FAILED`, `E_RUN_ID_MISSING`, `E_CAPSULE_MISSING`,
`E_COUNT_MISMATCH`, `E_TOOL_ORDER_MISMATCH`, `E_PRIVACY_VIOLATION`,
`E_OBSERVATION_MISMATCH`, `E_UNEXPECTED_OUTCOME`, `E_INTERRUPTED` and `E_HARNESS_INTERNAL`. The mock names every refused request
(`E_MODEL_MISMATCH`, `E_PROMPT_MISMATCH`, `E_ROLE_SEQUENCE`, `E_OBSERVATION_MISSING`,
`E_REQUEST_REPEATED`, …) and returns no `choices` with it. See the source for the full list.

## Tests

```sh
uv run pytest tests/test_example_evidence_lab.py          # unit, negative, real-capture
uv run pytest tests/test_example_blackbox_demo.py          # the original demo, unchanged
```

`tests/test_example_evidence_lab.py` drives the real `nova capture` twice (one run with a
relative `--output-dir`), and exercises wrong model / prompt / roles / scenario, a missing
or tampered observation, an unknown tool, malformed and oversized requests, invalid JSON,
an incomplete reply, a timeout, a taken port, an abnormal or hung `nova`, a non-loopback URL
and a real-looking credential in the environment.

## Not claimed

No replay fidelity (replay was not run), no signature or timestamp, no hermetic network
isolation, no production-scale or throughput claim, and no statement that the capsule's
decision is *correct* — only that the capsule's model and tool records match a description
of what was supposed to happen that is computed from the fixtures alone. Planned follow-up (D2, tracked in the public issue tracker): a
corrected variant against the same observations, `nova diff`, lineage, and an optional
signed-offline profile.
