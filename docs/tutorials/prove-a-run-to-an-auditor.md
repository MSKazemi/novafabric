# Prove what an agent did, six months later

**~20 minutes.** By the end you will have a signed, portable artifact that a
sceptical third party can verify on their own machine — with no access to your
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

## 1. Capture the run — September

Nothing about your application changes. You wrap the command:

```bash
nova capture python triage_agent.py --input incident-4471.json
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
  model-calls.jsonl     ← every model call, OTel GenAI semconv
  tool-calls.jsonl      ← every tool invocation and what it returned
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

> **Prompts and responses are not captured by default.** If your evidence needs
> them, opt in explicitly — and know that everything captured passes secret
> scanning first. A capsule missing its `redaction-proof.json` is **invalid** and
> cannot be exported. Verifiable redaction is a precondition here, not a feature.

## 2. Seal it — the same day

A folder you can edit proves nothing. Sealing signs a Merkle root over the
contents:

```bash
nova seal sign ~/.novafabric/capsules/01KZ9VZPFQB95A63AAMD2TC7XD
```

Now any later modification — one byte in one file — breaks verification. Signing
takes about 7 ms ([benchmarks](../benchmarks.md#2-novaseal-signing-latency)), so
there is no reason to defer it to a batch job that might not run.

For evidence that must survive a dispute about *when* it existed, add an RFC 3161
timestamp so the date comes from a third party rather than from your clock.

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
nova seal verify 01KZ9VZPFQB95A63AAMD2TC7XD
```

Verification needs no network, no server, and no NovaFabric account — because
there is no such thing. The auditor can run it themselves on their own laptop.
**That is the property that makes it evidence rather than an assertion.**

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

`mocked` goes further — it **re-runs the command** with every model and tool call
served from the capsule. No API keys, no tokens spent, no dependency on the
provider still offering that model. This is the mode that answers "would it do
the same thing again", and it works when the original model has been retired.

A replay is itself a capsule, so:

```bash
nova diff 01KZ9VZPFQB95A63AAMD2TC7XD 01KZ9VZX0A9KAXGDNP42QJF0Y4
```

```console
Diff: 01KZ9VZPFQB95A63AAMD2TC7XD → 01KZ9VZX0A9KAXGDNP42QJF0Y4
  changed=2  added=0  removed=0
```

A structural diff, not a metric comparison — *what changed in the execution*.

### Package it for someone who has never heard of NovaFabric

```bash
nova export-evidence 01KZ9VZPFQB95A63AAMD2TC7XD --out incident-4471-evidence/
```

An Evidence Bundle: the capsule, its signatures, the verification instructions,
and an in-toto DSSE statement. Hand over the folder.

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
  compromised.
- **A `mocked` replay is not a fresh run.** It shows the same code and inputs
  produce the same result *given the recorded external responses*. It cannot tell
  you what today's model would say.

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
[design-partner](../governance/design-partners.md) sign-offs and currently has
zero. If you would have to live with this format, this is the moment your input
changes it.
