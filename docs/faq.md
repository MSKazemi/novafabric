# FAQ — the longer answers

The [README FAQ](../README.md#faq) answers the four questions people ask *before*
trying NovaFabric: whether it sends data anywhere, whether you have to change your
code, whether it is free, and whether it is production-ready. Terms used here are
defined in the [Glossary](glossary.md).

**This page answers the questions people ask after that** — while evaluating it,
while it is misbehaving, and while deciding whether to contribute. Nothing here is
repeated from the README, so the two pages cannot drift apart.

- [Evaluating it](#evaluating-it)
- [Using it](#using-it)
- [When something goes wrong](#when-something-goes-wrong)
- [Trust, security, and data](#trust-security-and-data)
- [The project itself](#the-project-itself)
- [Contributing](#contributing)

---

## Evaluating it

### Should I use this, honestly?

Use it if you need to **prove, replay, or compare a run that already happened** —
audit evidence, reproducibility, regression triage, "what changed between Tuesday
and today".

Do **not** use it if what you want is live dashboards and alerting. That is a
different job and other tools do it better. The honest comparison, including where
NovaFabric loses, is in [How NovaFabric compares](comparison.md). A wrong
recommendation costs you days and costs this project the only currency it has.

### What is the fastest way to find out if it fits?

An afternoon, not a reading session. The five-step evaluation — including the two
steps that actually matter (prove the redaction works; break NovaFabric and confirm
your workload survives) — is in
[For platform teams](for-platform-teams.md#how-to-run-a-real-evaluation-in-an-afternoon).

### How much does capture slow my run down?

Measured numbers, with the command and hardware stated so you can reproduce them
rather than take our word, are in [Benchmarks](benchmarks.md). Re-run them on your
own hardware before quoting them to anyone — that is what they are for.

### Can I use it in a commercial product?

Yes. Apache-2.0, including patent grant. You do not owe this project anything for
using it commercially, and there is no paid tier to upgrade to.

### Is it safe to depend on with one maintainer?

That is the correct question to ask, and the answer is "with your eyes open". The
risk is real; the mitigations are that capsules are plain folders readable without
this tool, there is no hosted dependency to lose, and the path to becoming a
maintainer is written down and open. The full discussion, including the case for
*not* adopting yet, is in
[For platform teams](for-platform-teams.md#the-risks-we-would-raise-if-we-were-you).

---

## Using it

### Which of the 32 extras do I actually need?

Run `nova doctor --check-extras`. It lists each extra as complete or incomplete,
names the distributions missing from each, and prints the exact
`pip install 'novafabric[<extra>]'` command. It exits 0 either way — most installs
deliberately omit most extras, so incompleteness is information, not failure.

### Do I need server mode?

Almost certainly not, to start. Local mode — capture, validate, replay, diff,
lineage — works today (beta, formats not frozen before v1.0) and needs nothing but the
CLI. Server mode (`nova server start`) exists for multi-user deployments and is
`experimental`; so is the cluster-scale tier, which does not feed the server today.
Postgres is never required for local mode.

### What is the difference between `nova serve` and `nova server start`?

`nova serve` is the single-user dashboard over your local capsule directory: it binds
to `127.0.0.1`, needs `--experimental` and the `serve` extra, and its mutations
(register, promote, redact, export) are confirm-gated and audit-logged.
`nova server start` is the multi-user REST API (server mode): SQLite by default or
Postgres, OIDC / offline-token / API-key authentication and RBAC
(`reader`, `writer`, `admin`, `auditor`). Both are experimental. See
[Local mode and server mode](concepts.md#local-mode-and-server-mode).

### Can I capture something that is not Python?

Yes. `nova capture <command>` wraps *any* command. For capturing model traffic from
a non-Python client, use `nova api-proxy` or `nova mcp-proxy`, which work at the wire
level and are language-agnostic.

### Where does everything get written?

Local mode writes to `~/.novafabric/` (or `$NOVAFABRIC_HOME`) — capsules under
`capsules/<ulid>/`, replays under `replays/<ulid>/`, the registry and lineage index in
`registry.db`. Up to v0.106.0 replays landed in `./.novafabric/replays/` relative to
where you ran the command. `NOVAFABRIC_CAPSULE_DIR`, when set, wins over
`$NOVAFABRIC_HOME/capsules/`, and `nova capture -o <dir>` overrides both for one run.
If capsules land somewhere unexpected, check for a stale `NOVAFABRIC_CAPSULE_DIR`
exported in your shell. Every variable is in the
[CLI reference](cli-reference.md#environment-variables).

### Can I move a capsule to another machine?

That is the entire point. A capsule is a self-contained folder. Copy it, tar it, put
it in object storage, email it to an auditor — `nova validate`, `nova verify` and the
`forensic`, `semantic` and `exact` replay modes work on it anywhere the CLI runs,
offline. `mocked` replay re-runs the recorded command, so the other machine also needs
the workload's code and dependencies; differences from the recorded `env.lock` are
reported as `env_warnings`. For a third party, export a signed
[Evidence Bundle](concepts.md#evidence-bundle) instead of the bare folder.

### Which replay mode should I use?

| Mode | Use when | Maturity |
|---|---|---|
| `forensic` | You want to inspect without executing anything — read-only, no network, no subprocess | works today |
| `mocked` (default) | You want to re-run the command with recorded responses served from the capsule: OpenAI and Anthropic calls (sync or async, streamed or not), MCP tool results, and — experimental — `record.tool` functions, framework tools through the adapters, and other registered model hosts. Anything else runs live; network use is observed, and a strict replay fails on a connection past loopback after the fact | works today (Python workloads, supported surfaces only) |
| `semantic` | You want a text-similarity score over the capsule's recorded responses — it does not re-execute and makes no model call | works today |
| `exact` | You want to know whether the run is *eligible* for a byte-exact re-run — it checks and reports reasons; it does not re-execute | works today |
| `intervention` | You want to ask "if this one recorded event had been different, would the outcome change?" — substitutes one event from an `InterventionSpec` and re-runs under mocked semantics | experimental |

To block the network and keep writes inside the working directory during a mocked
replay, add `--hermetic` (experimental, Linux only, needs `bwrap`). NovaFabric does
**not** claim exact replay of remote LLM calls. A remote model is not deterministic;
`mocked` and `semantic` exist precisely because pretending otherwise would be
dishonest. Full contract: [Replay modes](architecture/replay-modes.md).

### Why does `nova replay` refuse my capsule?

A strict `mocked` replay refuses before anything runs when it could not serve the
run faithfully, and names the reason: `CapsuleNotReplayable` (the capsule records no
command to re-run — for example one written by a framework adapter or the `@agent`
decorator without a usable process invocation, or imported from OpenTelemetry),
`ModelSurfaceNotServable` (a recorded model call on a surface replay cannot serve),
`ToolSurfaceNotServed` (LangChain tools observed but not servable from the capsule),
or `ToolOverrideUnenforceable` (a `replay.yaml` `allow: false` on a tool replay cannot
intercept). Run `nova replay <capsule> --dry-run` to see the decision without running
anything. Capsules captured since v0.109.0 also carry `replayability.json`, and
`nova capture` prints its verdict after `Capsule written`; add
`nova capture --require-replayable` to make capture exit `4` unless a strict replay
would serve everything (experimental). `--permissive` starts anyway, runs the
unservable calls live and counts them.

### My mocked replay exited 0 — did it reproduce the run?

Not necessarily. The replay `status` says how the replayed **process** ended. The
`fidelity` verdict in `replay_result.yaml` (experimental, new in v0.109.0) says
whether it **reproduced** the recording: exit code, normalized stdout, the files the
workload wrote, and the model and tool call sequence, with no divergence and no live
call. `nova replay --require-faithful` exits `4` unless the verdict is `faithful`. See
[Fidelity verdict](architecture/replay-modes.md#fidelity-verdict-adr-0321-experimental).

### Why does `nova lineage provenance` not show the dataset my run read?

Capture does not watch which files or datasets a run reads. A `consumed` edge exists
only when your code called `record_asset_consumption()` (`novafabric.registry.service`)
during the run, and `depends_on` edges come from an asset spec's `dependencies`. The
confidence labels (`observed`, `inferred`, `declared`) say where each edge came from:
see [Lineage Graph](concepts.md#lineage-graph).

---

## When something goes wrong

### I mistyped the command and `nova capture` still wrote a capsule

That is deliberate: a run that never started is evidence too. When the runner could
not start the workload (for example, the command does not exist), `nova capture`
prints `✗ Workload never started: <reason>` before the `Capsule written` line and
exits `127`. A workload that ran and failed gets only the `✗ Capsule written` line
and exits with the workload's own code.

### `nova verify` says "No .seal/ directory found"

Sealing is opt-in, so a default capture is unsealed and `nova verify` exits `1` on it.
Run `nova seal init` once (it creates a local, self-asserted ECDSA P-256 identity and
a `novaseal.yaml` with no network access), then capture again. Sealing never blocks
a capture: if it fails, `nova capture` prints `✗ Capsule not sealed: …` and keeps the
capsule. See [Sealing and verification](architecture/sealing-and-verification.md).

### What is the difference between `nova validate` and `nova verify`?

`nova validate` checks that a capsule is well-formed: `capsule.yaml`, `env.lock` and
`redaction-proof.json` against their schemas, no links or special files inside, and
no capture-time tampering recorded. It needs no key. `nova verify` checks the NovaSeal
seal: the signature, the RFC 3161 timestamp when present, Merkle log inclusion, and
that every file still matches the digest the seal signed; when one does not, it names
the first divergent file. An unsealed capsule can pass `nova validate` and fail
`nova verify`.

### `nova export-evidence` says `--key is required`

An Evidence Bundle is always signed with a local Ed25519 key, and there is no default
key until you create one. Run `nova init`, which writes
`$NOVAFABRIC_HOME/keys/signing_key.pem` (mode 600), and pass it with `--key`. This
key is separate from the NovaSeal identity that `nova seal init` creates.

### ~30 tests fail right after I clone and set up

You ran `uv sync` instead of `uv sync --all-extras`. A plain sync strips the optional
extras and those tests fail with import errors that look like your fault but are not.
Re-sync with `--all-extras`.

### `nova validate` warns about my `environment` value

The conventional set is `{development, test, staging, production}`. A custom value
like `ci` is legitimate and recorded verbatim, but validate warns on anything outside
that set — deliberately, because a warning on every single run just teaches people to
ignore warnings.

### `make test-changed` always fails

It is disabled on purpose and always exits `1`: pytest-testmon cannot parallelise
and took more than ten minutes on this suite. Use `make test-direct` (the tests that
directly cover your change), `make test-impacted` (everything whose import closure
reaches it) or `make test-fast` (the parallel suite, no Docker) instead.

### The test suite hangs

There is a 900-second `pytest-timeout` cap so a hang fails by name rather than
running forever. Read which test the timeout names; do not restart blindly.

### A command printed `pip install 'novafabric'` with no extra in it

That was a real bug, fixed in the `nova doctor --check-extras` work: Rich read
`[serve]` as a markup tag and silently deleted it. If you are on a version that still
does this, the extra name you want is the one named in the surrounding text — and
please [open an issue](https://github.com/MSKazemi/novafabric/issues/new/choose) so
we know it survived somewhere.

### Something else

Search [existing issues](https://github.com/MSKazemi/novafabric/issues) first, then
ask in [Discussions](https://github.com/MSKazemi/novafabric/discussions) — questions
belong there, and you will get a faster answer than in an issue. Open an issue when
you have something actionable: a bug with a reproduction, or a defined feature.

---

## Trust, security, and data

### How do I verify redaction actually happened?

Do not trust it — test it. Have the workload print, or send to a model, a fake key
in a format the scanner knows (for example a string shaped like an OpenAI or GitHub
token), capture the run, then grep the whole capsule for that string. Every capsule
also carries `redaction-proof.json`, which records what was scanned and what was
redacted. If you find a leak of a format the scanner claims to detect, that is a
security issue: follow [SECURITY.md](../SECURITY.md) and do **not** open a public
issue.

### Does a lower capture level keep my prompts out of the capsule?

No. Model-call capture stores the full request messages and response text in
`model-calls.jsonl` at every capture level, including `minimal` and the default
`standard` — mocked replay needs them. `NOVA_CAPTURE_LEVEL` changes only what the
adapter, tool-hook and `record` paths keep (digests at `standard`, payloads at
`forensic` and `air_gapped`); `minimal` behaves like `standard`, and `air_gapped`
does not block the network. `NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off` stops storing the
HTTP-layer response bodies of generic model hosts (not the OpenAI, Anthropic, Ollama
and Bedrock surfaces), and request messages are stored either way. There is no
setting today that leaves model-call content out of a capsule, so treat a capsule as
you treat the conversation it captured. See
[Capture levels](concepts.md#capture-levels--what-a-capsule-stores-experimental).

### What does the secret scan not catch?

The built-in scanner (rule pack `gitleaks-core-v0`, in `novafabric.capture.secrets`)
matches **known key formats**: OpenAI, Anthropic, Hugging Face, Replicate, Langfuse,
LangSmith, Weaviate, Qdrant, Cohere, Together, Mistral and Pinecone keys, NovaFabric's
own API keys and webhook secrets, and — from pack 0.7.0, shipped in v0.105.0 — AWS and
GitHub credentials. That is why the project says *secret-scanned*, never
*secret-free*. It does **not** detect:

- passwords, PEM private keys, JWTs, or database connection strings with credentials;
- credentials of a provider that has no rule;
- an AWS secret access key with no key name next to it (a bare 40-character value
  looks like any other identifier);
- a pre-prefix Pinecone key, which is indistinguishable from a UUID.

`redaction-proof.json` records what was scanned, what matched, and what was redacted.
It is not proof that no secret remains. For other formats, add a masker (`--masker`,
experimental) and keep testing with planted values.

### What does "signed" actually mean here?

Different things at different layers, and it is worth being precise:

- **Capsules** are **unsealed** by default. Once you configure NovaSeal (`nova seal
  init`, experimental), capture writes a DSSE envelope over the manifest into
  `.seal/` (ECDSA P-256 by default), appends it to a local Merkle log, and adds an
  RFC 3161 timestamp only when a `tsa_url` is set. A Sigstore keyless signature is a
  separate, opt-in path (`nova seal sign --backend sigstore`). See
  [Sealing and verification](architecture/sealing-and-verification.md).
- **Evidence Bundles** (`nova export-evidence`) are always signed: in-toto DSSE
  attestations signed with a local Ed25519 key, verifiable offline.
- **Container images** are signed keylessly with cosign and carry SLSA build
  provenance plus an SBOM as OCI artifacts.
- **Air-gap bundles** are one tar whose members are inventoried in a DSSE-signed
  manifest.

A signature proves origin and integrity. It does not prove the contents are *correct*
— nothing does, and any tool claiming otherwise is selling something.

### What is the difference between a Run Capsule, a seal and an Evidence Bundle?

The **Run Capsule** is the folder `nova capture` writes: the evidence itself. A
**seal** (NovaSeal, experimental, opt-in) is written *into* the capsule's `.seal/`
directory and makes later edits detectable with `nova verify`. An **Evidence Bundle**
is a separate signed ZIP built *from* a capsule by `nova export-evidence`, holding a
copy of the whole capsule, its lineage edges, the JSON schemas and signed
attestations ([full list](architecture/sealing-and-verification.md#what-is-in-the-bundle)),
for handing to a third party who verifies it offline. They are the only two
top-level formats.

### Does NovaFabric certify us as compliant?

No, and it will refuse to pretend to. NovaFabric emits conformance **receipts** —
evidence that a control was exercised, with provenance — never verdicts. A verdict is
an auditor's job. [Assurance cases](assurance-cases.md) explains the distinction and
why it is load-bearing.

### Is there telemetry?

None. No accounts, no phone-home, no update check, no crash reporting. A consequence
worth stating plainly: we have no idea who uses this unless they tell us, which is
why [ADOPTERS.md](../ADOPTERS.md) exists and why it is genuinely useful to add
yourself.

---

## The project itself

### Why "capsule" and not "trace"?

A trace is a row in someone's database, and it answers "what happened". A capsule is
a portable folder you own, and it answers "can this be replayed, compared, and
proven". The noun is different because the unit of value is different.

### When is v1.0, and what changes?

v1.0 is the **schema freeze** — the point at which the on-disk Run Capsule and
Evidence Bundle formats become a stable contract. Until then, treat capsule internals
as internal. What should be in v1.0 is being argued about in the open right now, in
[What should v1.0 be?](https://github.com/MSKazemi/novafabric/discussions/10) — if you
have a stake in the format, that is the moment to argue, not after.

### Why is nearly everything labelled `experimental`?

Because it is, and the project has a standing rule against blurring the line. Every
feature carries exactly one of: **works today**, **experimental**, **planned**, or
**future design**. Take the labels literally — they are maintained on purpose, and
an over-claiming doc is treated here as a bug.

### Where do I find facts, logos and descriptions for writing about NovaFabric?

The [press & media kit](press-kit.md) collects them in one place, so an article, talk or
listing can reference NovaFabric without asking first or guessing at the facts.

### Who pays for this?

Nobody. There is no company, no funding, and no revenue. That is also why the most
useful thing an organization can do for NovaFabric is not money but people — see
[the bus-factor discussion](for-platform-teams.md#the-risks-we-would-raise-if-we-were-you).

---

## Contributing

### I want to help but I do not know this codebase

That is the normal case and it is planned for.
[Good first issues](https://github.com/MSKazemi/novafabric/labels/good%20first%20issue)
each name the files to touch, the acceptance criteria, the tests to write, and an
honest difficulty estimate — so you never have to guess where to start.
[CONTRIBUTING.md](../CONTRIBUTING.md) gets you from clone to pull request in about 15
minutes.

### Is a documentation or typo fix worth opening?

Yes, genuinely, and they are reviewed like real contributions. A doc that lied to you
is a bug report about the project's honesty, which this project cares about more than
most.

### Do I need to ask before starting?

No — for anything labelled `good first issue` or for fixing something that annoyed
you, just open the PR. Do ask first (in an issue or discussion) before large work,
public API changes, or a new dependency; that is where you would otherwise write 800
lines that get rejected on a design point.

### Can I contribute without writing code?

Yes, and it counts the same. Documentation, translations, bug reports with
reproductions, design feedback, reviews, and talks are all listed in
[CONTRIBUTORS.md](../CONTRIBUTORS.md), and they all count toward the
[maintainer path](governance/maintainer-criteria.md).

### Can I use an AI assistant to write my contribution?

Yes — see the policy in [AGENTS.md](../AGENTS.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md). The requirement is not *how* you wrote it but
that **you understand it, you tested it, and you can respond to review on it**. A PR
its own author cannot explain wastes the reviewer's time, and that is the only thing
being guarded against.
