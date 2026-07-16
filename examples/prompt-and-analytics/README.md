# prompt-and-analytics

**Use this when you want to:** see why managing prompts as versioned,
labeled registry assets — and analyzing the runs they produce, offline —
is worth it. Register a prompt twice, point the `production` label at v1,
capture one run per version as two A/B variants, then answer "which
variant is cheaper/faster, and what exactly changed?" without a server,
a dashboard, or a single LLM token.

Everything here is pure stdlib — no keys, no network. The surfaces used
(`nova prompt`, `nova label`, `nova query`, `nova view`, `nova trend`,
`nova session`, capture's `--variant`/`--session-id` flags) are all
**experimental** (ADR-0112/0113/0116/0122/0129/0130/0131).

## What it does

`agent.py` is a deterministic dummy agent. At runtime it resolves its
prompt from the NovaFabric registry — by deployment label
(`PROMPT_LABEL=production`, the default) or by pinned version
(`PROMPT_REF=support-triage@2`) — renders it against a fixed support
ticket, and "answers" deterministically. Under `nova capture` it
self-reports one synthetic model call (tokens, latency, cost — a pure
function of the prompt version, so runs differ in a known way) and one
tool call into the live capsule via `NOVAFABRIC_CAPSULE_DIR`. In a real
deployment the wire-level/MCP hooks record these for you; the
self-report only keeps this example dependency-free.

## Run it

Use an isolated home so the example doesn't touch your real registry:

```bash
export NOVAFABRIC_HOME=/tmp/nova-prompt-demo
RUNS=$NOVAFABRIC_HOME/runs
```

### 1. Register two prompt versions, label v1 `production`

```bash
nova prompt register support-triage \
  -t 'You are a support triage assistant. Classify the ticket into billing/bug/other and draft a one-line reply. Ticket: {ticket}' \
  --var ticket -m "first cut"
nova prompt register support-triage \
  -t 'You are a support triage assistant. Classify the ticket into billing/bug/other and draft a one-line reply. Be empathetic, acknowledge the issue, and mention the 24h SLA. Ticket: {ticket}' \
  --var ticket -m "add tone + SLA guidance"
nova label set prompt:support-triage production 1
```

```
✓ Registered prompt support-triage@1 (12ac865740fa…)
✓ Registered prompt support-triage@2 (a89b6646ad06…)
✓ moved production: (unset) → 1  (moved_by=you, at 2026-07-16T07:55:55Z)
```

The label is the deployment pointer: the agent asks for "whatever
`production` points at", and rollout/rollback is one `nova label set`
away — with an append-only audit log (`nova label history`).

### 2. Capture two runs — one per prompt version, two variants

```bash
SID=$(nova session new --kind workflow)

# Arm A: resolve the production label (→ v1)
PROMPT_LABEL=production nova capture --output-dir $RUNS \
  --experiment prompt-rollout --variant prompt-v1 --variant-source manual \
  --session-id $SID --session-sequence 0 \
  -- python examples/prompt-and-analytics/agent.py

# Arm B: pin the v2 candidate explicitly
PROMPT_REF=support-triage@2 nova capture --output-dir $RUNS \
  --experiment prompt-rollout --variant prompt-v2 --variant-source manual \
  --session-id $SID --session-sequence 1 \
  -- python examples/prompt-and-analytics/agent.py

# The session manifest stays the authoritative ordered index:
CAPS=($(ls -d $RUNS/*/))
nova session add $SID ${CAPS[0]}
nova session add $SID ${CAPS[1]}
```

Each capsule records the variant block verbatim (`experiment_id:
prompt-rollout`, `variant_id: prompt-v1/-v2` — record-only; NovaFabric
never assigns arms) and the agent's stdout pins the exact prompt used:

```json
"prompt_ref": "prompt:support-triage@1+sha256:12ac865740fa62a5…"
```

### 3. Analyze offline

**Which arm is cheaper/faster?** (`nova query`)

```bash
nova query \
  --select 'count() AS runs, avg(cost) AS avg_cost, p95(latency) AS p95_ms' \
  --group-by variant --capsule-dir $RUNS
```

```
variant    runs  avg_cost  p95_ms
---------  ----  --------  ------
prompt-v1  1     0.000279  130
prompt-v2  1     0.000429  170

2 row(s) — 2 capsule(s) scanned, engine duckdb, window …
```

**Save that question as a named view** (`nova view`)

```bash
nova view save cost-by-variant \
  --select 'count() AS runs, avg(cost) AS avg_cost, p95(latency) AS p95_ms' \
  --group-by variant --description 'Cost + latency per prompt variant'
nova view run cost-by-variant --capsule-dir $RUNS
```

```
Saved view 'cost-by-variant' -> …/views/cost-by-variant.yaml
view_hash: sha256:d2856f8b69e9…
```

**Latency over time** (`nova trend`)

```bash
nova trend --metric latency --stat p95 --since 7d --capsule-dir $RUNS
```

```json
{"metric": "latency", "unit": {"kind": "duration_ms", "stat": "p95"},
 "capsule_count": 2, "series": [… {"bucket": "2026-07-16", "value": 168.0, "n": 2} …]}
```

**The rollout as one multi-turn session** (`nova session show`)

```bash
nova session show $SID
```

```
Session 01KXMYTVKR… (workflow, created 2026-07-16T07:56:35Z)
 seq  run_id                      … run status  duration
   0  01KXMYTW33TTECAA7MQJM9116S  … success     2.5s
   1  01KXMYVA02DG3YEKF0H3XV8VGA  … success     1.4s
turns=2  resolved=2  missing=0  tampered=0  cost=0.0007 USD
```

**What exactly changed between the arms?** (`nova diff --group-by variant`)

```bash
nova diff --group-by variant ${CAPS[0]} ${CAPS[1]}
```

```
Variant groups (ADR-0116, recorded attribution):
  prompt-rollout/prompt-v1: …/01KXMYTW33TTECAA7MQJM9116S
  prompt-rollout/prompt-v2: …/01KXMYVA02DG3YEKF0H3XV8VGA
Cross-arm diff: prompt-rollout/prompt-v1 → prompt-rollout/prompt-v2

Diff: 01KXMYTW33… → 01KXMYVA02…
  changed=1  added=1  removed=1
```

### 4. Roll out (or back) with one label move

```bash
nova label set prompt:support-triage production 2 --reason "v2 wins on tone"
nova label history prompt:support-triage   # append-only audit log
```

The next `PROMPT_LABEL=production` run picks up v2 automatically — no
code change, and every past capsule still pins the exact
`version + content hash` it actually ran with.
