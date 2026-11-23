# EvidenceLab — one deterministic incident, captured for real

An offline, zero-key incident-investigation agent run under the **actual**
`nova capture`. It makes one genuine OpenAI SDK request to a strict loopback fixture
model and two genuine read-only tool calls, so the resulting capsule holds real
`model-calls.jsonl` and `tool-calls.jsonl` records — none are injected or painted.

> **Status — EvidenceLab D1 and D2 (issues #287 and #288).** Everything described here is
> **works today** for this example except where a label says otherwise. It is one
> scenario (`service-incident-v1`) with two controlled variants. Replay *fidelity* is
> **not evaluated** (only read-only forensic inspection runs), and the signed profile is
> a **self-asserted local identity**, not external identity validation. `record.tool` and
> `nova seal init` are **experimental** (ADR-0306 slice 1 and ADR-0301; see
> [replay modes](../../docs/architecture/replay-modes.md)).

## Run it — the walkthrough (D2)

```sh
uv sync --all-extras            # or: pip install novafabric openai
python examples/evidence_lab/run.py --scenario service-incident-v1 --output-dir evidence-lab-out
```

In a fresh, temporary NovaFabric home this registers the synthetic dataset
(`nova register`), captures **two real runs** of the agent — the *unsafe* variant and the
*corrected* variant — and then runs `nova inspect` (reads the asset back from the
registry), `nova lineage provenance`, `nova verify`, `nova replay --mode forensic` and
`nova diff` on them. Real output (paths shortened with `…`; run ids are the real ones):

```text
evidence-lab walkthrough: scenario service-incident-v1 (seed=42, profile=zero-key)
input: SYNTHETIC fixtures only (no real data); capture: actual `nova capture`, no record injected
unsafe: run_id 01M8375Q8Z8Q3M4Q6TD40AE9EG | logical model calls 1 | tool calls 2 | prompt prompt-v1 | decision 2e810ef91794…
corrected: run_id 01M83761F1V4P838449PA4YVE4 | logical model calls 1 | tool calls 2 | prompt prompt-v2-corrective | decision 0654b0be3da6…
  task      unsafe                     policy_oracle_verdict            FAIL          policy-rejected [as expected]
  capture   unsafe                     logical_calls_and_decision       PASS          capture-matches-fixtures [as expected]
  capture   unsafe                     run_declared_registered_asset    PASS          declared-consumption-of-registered-asset [as expected]
  integrity unsafe                     native_seal_verification         NOT_EVALUATED seal-not-configured [as expected]
  replay    unsafe                     replay_fidelity                  NOT_EVALUATED forensic-only [as expected]
  task      corrected                  policy_oracle_verdict            PASS          policy-accepted [as expected]
  capture   corrected                  logical_calls_and_decision       PASS          capture-matches-fixtures [as expected]
  capture   corrected                  run_declared_registered_asset    PASS          declared-consumption-of-registered-asset [as expected]
  integrity corrected                  native_seal_verification         NOT_EVALUATED seal-not-configured [as expected]
  replay    corrected                  replay_fidelity                  NOT_EVALUATED forensic-only [as expected]
  capture   both                       nova_diff_names_the_change       PASS          diff-matches-variants [as expected]
harness verdict: PASS (report: …/evidence-lab-out/evidence-lab-report.json)
a NOT_EVALUATED is not a pass; the unsafe task FAIL is the expected outcome
```

It took 24.0 s on a laptop (measured once; not a benchmark).

Each line is one check on one of four separate axes. **`as expected` is what makes the
harness verdict PASS** — not every check being `PASS`: the unsafe task is expected to
`FAIL`, and an unsealed capsule's integrity is expected to be `NOT_EVALUATED`. The harness
verdict also needs **every** required check to be present — 11 for zero-key, 16 for
signed-offline — so a step that never ran (say `nova diff`) is a `FAIL`, listed under
`missing_checks` in the report, not a quiet PASS of the checks that did run.

| Axis | unsafe | corrected | How it is decided |
|---|---|---|---|
| `task` | `FAIL` (expected) | `PASS` (expected) | the fixed policy oracle over the recorded decision; `reduce_max_connections` must be an integer in 100..900 |
| `capture` | `PASS` | `PASS` | 1 logical model call, 2 read-only tool calls, decision digest = the fixture reply's; the run's *declared* input is exactly the registered dataset (never empty), and `nova inspect` reads that asset back from the registry; `nova diff` names the one changed model call (request and response), two unchanged tool calls, no environment change, and exactly one changed output (the agent's stdout line, whose digests are computed from the fixtures) |
| `integrity` | `NOT_EVALUATED` | `NOT_EVALUATED` | `nova verify --json` reports `sealed: false`; `nova validate` passing does not change that |
| `replay` | `NOT_EVALUATED` | `NOT_EVALUATED` | forensic inspection ran (read-only); no fidelity claim |

The two variants read **the same observations** from the same two tools. Only the prompt
differs — the corrected one adds a corrective policy (`prompt-v2-corrective`) — and the
mock answers each variant's exact request with that variant's fixture reply. The mock is a
fixture, so the corrected decision is a *recorded fixture*, not model reasoning; what the
walkthrough shows is that capture and diff record the change faithfully, while lineage records the same declared input for both runs.

**Lineage is declared, not observed.** The agent declares its input with NovaFabric's SDK
call `record_asset_consumption`; the capsule's `assets.jsonl` and the `consumed` edge in
`lineage.jsonl` come from that declaration. NovaFabric's lineage writer labels the edge
`"confidence": "observed"`, which describes how the edge was recorded — nothing here
watches the agent read the dataset. Because the edge is built from the capsule alone and
never consults the registry, the check also reads the asset back with `nova inspect`: a
`nova register` that reported success without writing anything fails the check
(`asset-not-in-registry`).

A capture that breaks a D1 check (a missing or extra call, a changed digest, a payload in
a tool record) **stops the walkthrough**: the report then holds a `capture` `FAIL` check
for that variant whose `reason_code` is the `E_*` code, and the later steps do not run on
an untrustworthy capsule. A faithfully captured but *wrong* decision is different: it is
reported by that variant's `task` check failing against its expected verdict (for
example, a corrected reply outside the value bounds is `task corrected FAIL`, expected
`PASS`), and the harness verdict is `FAIL` (`E_CHECK_UNEXPECTED`).

### Signed offline (opt-in)

```sh
python examples/evidence_lab/run.py --scenario service-incident-v1 --output-dir out \
    --profile signed-offline --require-verified
```

Runs `nova seal init` in the disposable home *before* the captures, so both capsules are
sealed, and verifies each with `nova verify --json --ca-bundle <that local CA>`. It then
proves the verifier can fail: a tampered member, a missing member, a corrupted DSSE
signature and a different local CA must all be **refused** (each `negative:*` check is
expected to read `FAIL`), each for its own reason: the property the case broke
(`capsule_binding_ok`, `signature_ok` or `ca_chain_ok`) is `false` **and** the properties
it did not touch stay `true` (a corrupted signature also fails the chain check, so the
chain is not required intact there). A copy refused on every property at once proves
nothing and reads `refused-for-another-reason`. An untouched copy made the same way must still verify
(`control:pristine-copy`, expected `PASS`). The verifier's output for every case is kept
under `out/negative/`. The CA's public certificate is copied to
`out/trust/local-seal-ca.crt.pem` so you can re-verify later; private keys never leave the
temporary home, which is deleted. Measured trust semantics, as `nova verify` reports them:
`identity_trust: local-ca-pinned` (continuity with this installation — **not** who the
signer is) and `timestamp_trust: absent` (this profile configures no timestamp authority; outbound connections are not monitored).
An extra unsigned file added to a sealed capsule still verifies; that is the current
verifier contract, so it is not reported as tampering.

`--require-verified` makes the harness exit `2` unless **both** variants have an
integrity check and every one is `PASS` (a run that stopped before the second variant was
verified does not count as proven):
with the default zero-key profile it always fails (`E_PROOF_REQUIRED`), by design; with
signed-offline a failed verification already fails as an unexpected check
(`E_CHECK_UNEXPECTED`).

## One variant only (the D1 single run)

```sh
python examples/evidence_lab/run.py --scenario service-incident-v1 --variant unsafe \
    --output-dir evidence-lab-out
```

Real output of that command (paths shortened with `…`; the run id is the real one):

```text
evidence-lab: scenario service-incident-v1 (seed=42, profile=zero-key)
input: SYNTHETIC fixtures only (no real data)
capture: actual `nova capture` (NovaFabric 0.109.0); no record was injected by the harness
run_id: 01M82DJXXPPCVF891YCQEWR564
capsule: …/evidence-lab-out/capsules/01M82DJXXPPCVF891YCQEWR564
logical model calls: 1 (expected 1) | tool calls: 2 (expected 2: read_metrics -> search_logs)
task verdict: FAIL - policy oracle rejected 'disable_rate_limiting' (forbidden_action); this is the scenario's expected unsafe outcome
harness verdict: PASS - every D1 check held (report: …/evidence-lab-out/evidence-lab-report.json)
integrity: NOT_EVALUATED (no signer configured) | replay: NOT_EVALUATED (not part of D1)
```

It takes about 12 seconds on a laptop (measured once: 12.5 s; not a benchmark). No API
key and no network beyond `127.0.0.1` (validated, not enforced; see below). The harness
writes only under `--output-dir` and a temporary directory that is removed on exit — it
does not write bytecode beside the scripts either (`sys.dont_write_bytecode`).

### Four labels, never merged

| Label | What it tells you | This run |
|---|---|---|
| `input` | Whether anything real went in. | `SYNTHETIC` — fixtures written for this example, no real data. |
| `capture` | Where the capsule came from. | The actual `nova capture`; the harness only reads the result and injects nothing. |
| `task verdict` / `harness verdict` | The agent's decision against the policy, and whether every D1 check held (below). | `FAIL` / `PASS` |
| `integrity` / `replay` | Whether a seal was verified and whether replay was run. | `NOT_EVALUATED` / `NOT_EVALUATED` |

The same labels are in `evidence-lab-report.json` (`input`, `capture`, `axes`,
`harness_verdict`), including after a failed run.

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

**Independence of the oracle.** The agent, the mock and the harness share
[`evidence_scenario.py`](evidence_scenario.py), so a bug there could be produced and accepted
by the same code. The tests therefore pin the expectations as literals that do not come from
that module: the SHA-256 of each tool's expected result (re-derived with the standard library
only, straight from the fixture files), the committed bytes of `scenario.yaml` and of the
golden transcript, and a hand-written table of which action the policy must accept or
reject. The golden request itself is a committed file, not generated at run time.

**Value bounds (D2).** An allowlisted action is accepted only with a valid value:
`policy.value_bounds` limits `reduce_max_connections` to an integer from 100 to 900
(`value_out_of_bounds`, `value_not_an_integer` otherwise; a boolean is not an integer
here). The tests pin that table as literals, independently of the manifest.

A missing or extra record, a wrong order, a changed digest or a payload in a tool record
fails the harness with a named code. NovaFabric's own capture stays fail-open for the
workload; it is the *harness* that refuses to call a run good without the records.

## Privacy and isolation — what is and is not true

- **Tool records are digest-only** at the default (`standard`) capture level: `arguments`
  is `{}`, `result` is `null`, and the digests are what you compare. The harness never sets
  `NOVA_CAPTURE_LEVEL`, and it refuses (named `E_ENV_REJECTED`) a run whose environment
  asks for more than `standard`.
- **The model-call record is not digest-only.** At NovaFabric's *current default* capture
  level the request messages and the response are recorded in `model-calls.jsonl`; here that
  is the synthetic prompt, the fixture log lines and the fixture decision. This example
  neither changes that default nor relies on it being the right one: the project's
  documented privacy invariant is that full prompt/response capture is opt-in, so the current
  default does not yet match it, and that is decided separately. The harness needs the text
  to compare it with the golden transcript, and fails with a named code
  (`E_MODEL_RECORD_MISMATCH`) if a capsule ever lacks it, instead of passing. The example is
  synthetic-only by design. Do not point this harness at real data. `capsule.yaml` also
  records the command line, including the output paths you passed.
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
  timeout. The mock is single-threaded: a stalled client is dropped after its per-read
  timeout and any one connection after twice that, so a client dripping one byte at a time
  cannot hold it indefinitely.

## Layout

| File | Role |
|---|---|
| [`run.py`](run.py) | The harness: mock server + `nova capture` subprocess, isolation, evidence checks, report; the D2 walkthrough orchestration. |
| [`walkthrough.py`](walkthrough.py) | D2 judges (pure functions): task, capture, diff, lineage, integrity, negative proofs, replay; the four-axis summary. |
| [`agent.py`](agent.py) | The agent: two `@record.tool` boundaries, one SDK call, the policy review. |
| [`mock_llm_server.py`](mock_llm_server.py) | Strict loopback fixture model; refuses everything but the one expected request. |
| [`evidence_scenario.py`](evidence_scenario.py) | Shared manifest loader, the tools' simulator functions, request builder, policy oracle, URL/env guards. |
| [`scenarios/service_incident_v1/`](scenarios/service_incident_v1/) | The manifest, four fixtures, the corrected variant's prompt and reply, and one golden request transcript per variant. |

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
| `prompt-corrected.txt` (corrected variant) | `65d1ba14c072898f300f9041a05ebe6a748d353659862dcb89e223d6aa341d19` |
| `model_response-corrected.json` (corrected variant) | `fd4022ac617092e310ca18c15b0f7e88f898362f8bb17f21173a50c1010c2a26` |

The corrected variant's canonical request (`golden/model_request-corrected.json`) hashes to
`1cd0f95a1b0c463f2abf2c9386534758b348373c815d23ba7b510512668d5948`; the two fixture
decisions hash to `2e810ef9…184c` (unsafe) and `0654b0be…c336` (corrected).

The canonical request body (`golden/model_request.json`, canonical JSON) hashes to
`a2d3ab9c3f24d3a9961a44ec3349a5fa3fe241073ab6fdaf955f2804efab0226`. Two runs of the same
variant share every canonical hash in the report (`fixture_sha256`, `request_sha256`,
`tool_sequence_sha256`, `decision_sha256`); only run ids, timestamps, paths,
`elapsed_seconds` and the mock's `mock_pid` differ. The unsafe and corrected variants
differ by design: the corrected variant uses a different prompt revision and a different
recorded decision, so its `request_sha256` and `decision_sha256` are not the unsafe ones. The loader refuses a
modified fixture (`E_FIXTURE_DIGEST_MISMATCH`), so keep line endings as committed.

## Exit codes and failure names

`run.py` exits `0` (harness PASS), `2` (a named failure: `evidence_lab: FAIL code=E_…` on
stderr and in the report), or `77` (**SKIPPED**: the optional `openai` SDK is not
installed — no evidence was produced). Failures include `E_MOCK_PORT_IN_USE`,
`E_CAPTURE_TIMEOUT`, `E_CAPTURE_OUTPUT_TOO_LARGE` (a command printed more than 1 MiB and was stopped), `E_CAPTURE_FAILED`, `E_RUN_ID_MISSING`, `E_CAPSULE_MISSING`,
`E_COUNT_MISMATCH`, `E_TOOL_ORDER_MISMATCH`, `E_PRIVACY_VIOLATION`,
`E_MODEL_RECORD_MISMATCH`, `E_OBSERVATION_MISMATCH`, `E_UNEXPECTED_OUTCOME`, `E_INTERRUPTED`
and `E_HARNESS_INTERNAL`; the walkthrough adds `E_ASSET_REGISTER_FAILED`,
`E_SEAL_INIT_FAILED`, `E_CHECK_UNEXPECTED` (a check's verdict differs from the expected one;
the report lists which) and `E_PROOF_REQUIRED` (`--require-verified` without a verified
seal for every run), and the loader `E_VARIANT_UNKNOWN`. The mock names every refused request (`E_MODEL_MISMATCH`,
`E_PROMPT_MISMATCH`, `E_ROLE_SEQUENCE`, `E_OBSERVATION_MISSING`, `E_REQUEST_REPEATED`,
`E_BODY_TRUNCATED` when fewer bytes arrive than `Content-Length` declared, …) and returns no
`choices` with it. See the source for the full list.

## Tests

```sh
uv run pytest tests/test_example_evidence_lab.py          # unit, negative, real-capture
uv run pytest tests/test_example_evidence_lab_walkthrough.py  # D2: judges + real walkthroughs
uv run pytest tests/test_example_blackbox_demo.py          # the original demo, unchanged
```

`tests/test_example_evidence_lab_walkthrough.py` drives every judge's `FAIL` path with a
broken input (mutated decision digest, missing tool or model call, empty or foreign
lineage, an unexpected diff, a sealed-but-tampered or wrong-CA verify result, a false
green, a refusal for several reasons at once, an asset missing from the registry, an
environment or output drift in the diff, one variant left unverified), runs the real
walkthrough zero-key, with `--require-verified` and signed-offline, and re-verifies the
sealed output itself with `nova verify` before and after tampering a copy. Real-run
negatives: a corrected reply outside the bounds (a `task` FAIL against expected `PASS`), a
`nova register` that claims success but writes nothing (`asset-not-in-registry`), and a
missing or FIFO golden transcript (a named error, not a traceback or a hang).

`tests/test_example_evidence_lab.py` drives the real `nova capture` twice (one run with a
relative `--output-dir`), and exercises wrong model / prompt / roles / scenario, a missing
or tampered observation, an unknown tool, malformed and oversized requests, invalid JSON,
an incomplete reply, a timeout, a taken port, an abnormal or hung `nova`, an interrupt while
the workload runs in its own session, an oversized or FIFO capsule stream, a non-loopback URL
and a real-looking credential in the environment.

## Not claimed

No replay fidelity: only `nova replay --mode forensic` runs, which is read-only inspection
and re-executes nothing, so the `replay` axis stays `NOT_EVALUATED`. Since NovaFabric 0.109.0 `nova capture` also writes
`replayability.json`; for this run it classifies the one model call as servable and the two
tool calls as `tool_result_not_servable`, because tool arguments and results are digest-only below
the `forensic` capture level. That is a statement about the capsule, **not** a replay result,
and this example neither reads nor asserts it. No signature by default and no timestamp in
either profile; the signed-offline identity is self-asserted. No hermetic network
isolation, no production-scale or throughput claim, no regulatory certification, and no
statement that a capsule's decision is *correct* — a valid seal proves the records were not
changed after sealing, not that the decision was right. What is shown is that the
capsules' model and tool records match a description of what was supposed to happen that
is computed from the fixtures alone. Dataset consumption is declared by the agent
through `record_asset_consumption`, not observed: the lineage check proves the declaration
was recorded, names an asset that `nova inspect` reads back from the registry, and agrees
with `nova lineage provenance` — not that the agent read that asset (NovaFabric's
`"confidence": "observed"` edge label does not change that). The reported decision digest
is read from the agent's result file, which is outside the capsule and its seal; the
capsule's own model response is separately checked against the fixture reply.
