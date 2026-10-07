# Prove a recorded agent run has not been edited, six months later

**~20 minutes.** By the end you will have a signed, portable record of an agent run
that a sceptical third party can verify on their own machine — with no access to your
infrastructure, no running server, and no network.

This is the scenario NovaFabric exists for. Everything else in the docs is
mechanism; this is the point.

---

## The situation

It is March. An AI agent made a decision that someone is now questioning — a
declined application, a flagged transaction, a mis-triaged incident. The run
happened in September.

You are asked, in some form:

> *"Show us exactly what the system did, prove the record has not been edited,
> and demonstrate that you would get the same result again."*

Your tracing backend sampled most of it away. What survived is rows in a database
you control — which is precisely why it does not settle the question. **Evidence
that only you can produce, from a system only you can write to, is not evidence.**

What follows produces something better.

---

## 0. Once: give NovaFabric a signing key

Sealing is **opt-in**: a capsule is only signed when a `novaseal.yaml` exists in your
NovaFabric home when the run is captured. Set it up once, before September:

```bash
openssl ecparam -name prime256v1 -genkey -noout \
  | openssl pkcs8 -topk8 -nocrypt -out ~/.novafabric/seal.key
openssl req -new -x509 -key ~/.novafabric/seal.key -out ~/.novafabric/seal.crt \
  -days 3650 -subj "/CN=triage-agent evidence"

cat > ~/.novafabric/novaseal.yaml <<'EOF'
profile: local
key_path: ~/.novafabric/seal.key
cert_path: ~/.novafabric/seal.crt
EOF
```

This is a self-signed key for the walkthrough; [key management](../novaseal-key-management.md)
covers KMS/HSM keys and certificates issued by your own CA. Without this step the
capsule below is still written and still verifiable as a folder, but it is **not
signed**, and `nova verify` will say so.

## 1. Capture the run — September

Nothing about your application changes. You wrap the command:

```bash
nova capture -- python triage_agent.py --input incident-4471.json
```

```console
✓ Capsule written: ~/.novafabric/capsules/01KZ9VZPFQB95A63AAMD2TC7XD
  (run_id=01KZ9VZPFQB95A63AAMD2TC7XD)
```

That directory is the whole artifact:

```
01KZ9VZPFQB95A63AAMD2TC7XD/
  capsule.yaml          ← the run manifest: id, status, timing, exit code
  trace.jsonl           ← the execution span tree
  model-calls.jsonl     ← each captured model call, OTel GenAI semconv
  tool-calls.jsonl      ← tool calls seen by the MCP hook or a framework adapter
  env.lock              ← the exact environment: packages, versions, platform
  redaction-proof.json  ← proof that secret scanning ran and what it removed
  lineage.jsonl         ← what this run consumed and produced
  inputs/ outputs/      ← the actual data in and out
```

**Two properties matter for what comes next.** It is a plain folder, so you can
`tar` it and put it in the same archive as everything else you retain. And it is
complete on failure too — a crashed run produces a capsule with
`status: failure` and an `error` block, which is usually the run someone asks
about.

> **Prompts and responses are in the capsule.** For a Python workload, each captured
> model call's request messages and response text are written to `model-calls.jsonl`
> — on your machine, after the built-in secret scan (14 API-key and token rules; PII
> masking is a separate opt-in). Nothing is sent anywhere. A capsule missing its
> `redaction-proof.json` is **invalid** and cannot be exported.

## 2. It is sealed as it is written

A folder you can edit proves nothing. Because step 0 configured a key, the capture
already signed the capsule: a DSSE signature over the manifest, which carries a
SHA-256 for every file, plus an entry in a local Merkle log whose inclusion proof
travels inside the capsule (`.seal/`). Nothing to remember, no batch job that might
not run. Signing takes about 7 ms
([benchmarks](../benchmarks.md#2-novaseal-signing-latency)).

Now any later modification — one byte in one file — breaks verification.

For evidence that must survive a dispute about *when* it existed, add a `tsa_url` to
`novaseal.yaml` so each seal gets an RFC 3161 timestamp from a third party rather
than from your clock (opt-in; see [NovaSeal configuration](../novaseal-configuration.md)).

## 3. Archive it — and then forget about it

```bash
tar czf incident-4471-run.tar.gz \
  -C ~/.novafabric/capsules 01KZ9VZPFQB95A63AAMD2TC7XD
```

Put it wherever you keep records. **There is nothing to keep running.** No
database to migrate, no server whose EOL matters, no vendor whose pricing or
existence you now depend on. That is the whole design.

---

## 4. March: answer the question

### "Show us exactly what the system did"

```bash
tar xzf incident-4471-run.tar.gz
nova validate 01KZ9VZPFQB95A63AAMD2TC7XD
```

```console
✓ Valid capsule: 01KZ9VZPFQB95A63AAMD2TC7XD  status=success
```

Then read it. `trace.jsonl` is the span tree; `tool-calls.jsonl` is what the agent
actually called and what came back. These are line-delimited JSON — `jq` works,
and so does a spreadsheet. **Deliberately not a proprietary format**, because a
format only your tool can read reproduces the original problem.

### "Prove the record has not been edited"

```bash
nova verify 01KZ9VZPFQB95A63AAMD2TC7XD
```

```console
  ✓ Signature (DSSE ECDSA P-256): OK
  ⊘ Timestamp (RFC 3161): NOT PRESENT (TSA skipped or unavailable)
  ✓ Merkle log inclusion: OK
  ✓ Manifest binding (capsule.yaml == signed payload): OK
  ✓ Evidence binding (per-file sha256): OK
```

Verification needs no network, no server, and no NovaFabric account. The auditor can
run it on their own laptop: the Merkle inclusion proof is carried in the capsule, so
your log is not needed. Change one byte of `outputs/stdout.txt` and the evidence
binding fails, with a non-zero exit.

**Who signed it is a separate question.** Run as above, verification checks the
signature against the certificate stored *in* the capsule: it proves the record is
unchanged since that key signed it, not whose key it was. To bind the signature to
your organisation, sign with a certificate issued by a CA the auditor trusts and have
them pass it: `nova verify … --ca-bundle your-ca.pem` (experimental; a self-signed
signing certificate like the one in step 0 is rejected there by design).

### "Demonstrate you would get the same result again"

```bash
nova replay 01KZ9VZPFQB95A63AAMD2TC7XD --mode forensic
```

```console
✓ Replay written: .novafabric/replays/01KZ9VZX0A9KAXGDNP42QJF0Y4
  (replay_id=01KZ9VZX0A9KAXGDNP42QJF0Y4  mode=forensic)
```

`forensic` is read-only: no network, no subprocess, nothing re-executes. It
reconstructs what happened from the record. Use it when the environment must not
be touched.

`mocked` goes further — it **re-runs the command** with the recorded model responses
served from the capsule, so no live model call is made and the original model need
not exist any more. Two limits matter for this question. **Tool calls are not
substituted:** they run live, against today's systems. And only synchronous OpenAI
and Anthropic chat calls are served from the capsule; async, streaming and the
OpenAI Responses API are not intercepted. So `mocked` answers "given the same model
replies, does the code take the same path?" — only as far as its tools behave as
they did in September.

To show what changed between two executions, capture the same input again and diff
the two capsules:

```bash
nova capture -- python triage_agent.py --input incident-4471.json
nova diff 01KZ9VZPFQB95A63AAMD2TC7XD 01KZB2C8M3Q4R5S6T7V8W9X0YZ
```

A structural diff, not a metric comparison — *what changed in the execution*: model
calls paired in order, tool calls by name and arguments, environment and outputs.

### Package it for someone who has never heard of NovaFabric

```bash
nova export-evidence ~/.novafabric/capsules/01KZ9VZPFQB95A63AAMD2TC7XD \
  --output incident-4471-evidence.zip --key ~/.novafabric/keys/signing_key.pem
```

An Evidence Bundle: a ZIP with the capsule, its seal, a manifest of file hashes and
an in-toto DSSE statement, signed with the Ed25519 key `nova init` created. Hand over
the file.

---

## What this does *not* prove

Being precise here matters more than in most docs, because someone may rely on it.

- **It does not prove the decision was correct.** It proves what the system did,
  not that it should have.
- **It does not make you compliant** with the EU AI Act, ISO 42001, GDPR, or
  anything else. It produces evidence that *supports* those workflows. The
  exporters map captured facts into a required shape; they do not certify.
- **It attests only that the capsule is unmodified since signing.** It says
  nothing about whether the inputs were honest or the environment was already
  compromised — and a key holder can sign a false record.
- **It does not prove the record is complete.** Capture records what it is wired to
  see: hooked model SDKs, MCP tool calls, adapters, stdout/stderr and the
  environment. Work the agent did through other channels is not in the capsule.
- **A `mocked` replay is not a fresh run.** It serves the recorded model responses,
  but tools run live. It cannot tell you what today's model would say, and it is
  only as faithful as the tools are stable.

Claiming more than this is exactly the overclaiming the project is built to make
impossible.

---

## Where to go next

- [Concepts](../concepts.md) — the replay modes in depth, and when each is honest
- [Architecture](../architecture.md) — the design invariants behind these guarantees
- [Benchmarks](../benchmarks.md) — the cost of capture and sealing, reproducibly
- [Comparison](../comparison.md) — when a different tool is the right answer

**Does your organization run workloads like this?** Freezing the v1.0 capsule
format requires three independent
[design-partner](../governance/design-partners.md) sign-offs, and the format is not
frozen yet. If you would have to live with this format, this is the moment your
input changes it.
