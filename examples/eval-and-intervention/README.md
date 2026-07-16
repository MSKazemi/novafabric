# Evidence-grade eval + intervention replay example

**Use it to see:** the zero-token eval loop (`nova eval offline`,
`nova diff --significance`, `nova eval contamination-check`), intervention
replay (`nova replay --mode intervention`), and the Accountability Spine
(`nova energy`, `nova ledger`, `nova safety-case`) — all offline, no LLM,
no network, pure stdlib. All of these surfaces are **experimental**.

Full walkthroughs: [feature tour §18–§20](../../docs/tutorials/feature-tour.md#18-run-an-evidence-grade-eval-loop--zero-tokens).

## What's in here

```
eval-and-intervention/
├── agent.py            # stdlib "agent": 3 deterministic tool calls, self-reported
├── out.schema.json     # JSON-schema contract for `nova eval offline --check contract`
├── check-spec.yaml     # metamorphic check-spec (whitespace-variant consistency)
├── intervention.yaml   # InterventionSpec for `nova replay --mode intervention`
└── make_scores.py      # synthetic scores.jsonl history for `nova diff --significance`
```

The agent performs two word-counts (one input a whitespace variant of the
other) and one lookup, and — when running under `nova capture` — appends each
call to the live capsule's `tool-calls.jsonl` via `NOVAFABRIC_CAPSULE_DIR`.
In a real deployment the MCP hook, `nova mcp-proxy`, or a capture-hook plugin
records tool calls for you; the self-report only keeps this example free of
third-party dependencies.

`AGENT_MODE=regressed` plants a consistency bug: the whitespace-variant input
is miscounted, which the metamorphic check catches with zero model calls.

## Quick start

```bash
mkdir -p /tmp/nova-tour
AGENT_MODE=baseline  nova capture --output-dir /tmp/nova-tour python examples/eval-and-intervention/agent.py
AGENT_MODE=regressed nova capture --output-dir /tmp/nova-tour python examples/eval-and-intervention/agent.py
CAPS=($(ls -d /tmp/nova-tour/*/)); BASE=${CAPS[0]}; REG=${CAPS[1]}

# zero-token checks: coverage / contract / metamorphic
nova eval offline --capsule $BASE --check coverage --declared-tools word_count,lookup
nova eval offline --capsule $BASE --check contract \
    --schema examples/eval-and-intervention/out.schema.json --field output
nova eval offline --capsule $REG  --check metamorphic \
    --spec examples/eval-and-intervention/check-spec.yaml    # → False (planted regression)

# statistically-gated regression diff (exit 3 = significant regression)
python examples/eval-and-intervention/make_scores.py /tmp/nova-tour/scores
nova diff --significance --baseline /tmp/nova-tour/scores/baseline/scores.jsonl \
    --candidate /tmp/nova-tour/scores/candidate/scores.jsonl --metric task_pass

# counterfactual replay: "what if the second tool call had answered 3?"
nova replay $BASE --mode intervention \
    --intervention-file examples/eval-and-intervention/intervention.yaml \
    --output-dir /tmp/nova-tour/replays
```
