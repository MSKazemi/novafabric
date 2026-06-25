# Why NovaFabric? A plain-English guide

> **Who this is for:** engineers who have heard "AI observability" but aren't sure
> why they need it, or what it actually buys them day-to-day.

---

## The core problem: AI agents have no memory of themselves

When a traditional program runs, you have logs, stack traces, and deterministic
behavior. When an AI agent runs, you get output — and then it's gone.

Two weeks later someone asks:

- "Why did the agent diagnose that SLURM job as an OOM failure instead of an MPI
  error?"
- "Did the agent's recommendations change after we updated the system prompt?"
- "Which analyses do we need to re-run now that we found a corrupt sensor dataset?"

Without infrastructure, the answer to all three is: **"I don't know."**

NovaFabric fixes this. Every agent run becomes a **capsule** — a tamper-evident
record of exactly what happened, preserved forever, queryable at any time.

---

## Five things you can do that you couldn't do before

### 1. Capture — "What exactly happened?"

**The situation:** Your IoT anomaly detection agent files a report: "vibration sensor
spiked at 14:32, probable bearing failure." Three months later, maintenance disputes
it. What code ran? What data did the model actually see? What did it output before
writing the report?

**Without NovaFabric:** Gone. You have the report file and nothing else.

**With NovaFabric:**

```bash
nova capture python workloads/iot_detective/agent.py
```

This records the full run as a capsule: model name, temperature, every prompt sent,
every response received, every tool call and its result, wall-clock timing, exit code.

```bash
nova validate capsules/01KR9Q2AD…   # check the capsule is well-formed
cat capsules/01KR9Q2AD…/events.jsonl  # read the full trace
```

Three months later, you open the capsule and read exactly what happened.

---

### 2. Diff — "Did the agent's behavior change?"

**The situation:** You updated the `hpc-analyst` system prompt to be more concise.
Did the actual diagnoses change? Are the tool calls the same? Does it still cite
the right log lines?

**Without NovaFabric:** You'd have to run it again and eyeball the output manually.

**With NovaFabric:**

```bash
nova diff capsules/01KR9Q2A…  capsules/01KRB4F7…
```

You get a structured comparison:

```
model:         same  (qwen3:35b)
tools_called:  same  (read_log, write_diagnosis)
tool_args:     CHANGED — read_log now reads 40 lines, was 100
output_length: CHANGED — 312 tokens → 180 tokens
classification: same (OOM)
```

You can see immediately whether the prompt change caused a behavioral regression —
even if the final label looks the same.

---

### 3. Forensic Replay — "Can I reproduce this result?"

**The situation:** The HPC team disputes a diagnosis from last Tuesday. They say the
agent hallucinated a line number. You want to prove the output is reproducible —
or find out if something drifted.

**Without NovaFabric:** You'd have to hope the same log file is still there, hope
the model hasn't changed, and re-run manually.

**With NovaFabric:**

```bash
nova replay --mode forensic capsules/01KR9Q2AD…
```

This re-drives the agent against the *exact same inputs* stored in the capsule —
same log content, same model config, same tool responses. If the agent produces the
same diagnosis, it's reproducible. If it doesn't, something drifted (model update,
tool behavior change, non-determinism).

The replay itself becomes a new capsule, so you can `nova diff` original vs replay.

---

### 4. Lineage — "Which runs are affected by this data problem?"

**The situation:** You discover that `slurm-logs/job-42819-oom@v1` contained
truncated output — the last 200 lines were missing. Which agent runs read that file?
Which diagnoses might be wrong?

**Without NovaFabric:** You'd have to grep through logs hoping something is
recorded, or just assume everything is suspect.

**With NovaFabric:**

Each agent declares what it consumed:

```python
# inside hpc_analyst/agent.py
record_consumed("slurm-logs/job-42819-oom@v1")
```

Now open the dashboard Lineage tab, click `slurm-logs/job-42819-oom@v1`, switch to
**Blast radius** mode. Every run that consumed that file is highlighted. You re-run
exactly those capsules with the corrected log.

```bash
nova lineage blast-radius slurm-logs/job-42819-oom@v1
# → run:01KR9Q2AD…  run:01KRB4F7…  run:01KRC3X9…
```

---

### 5. Evidence Bundle — "Prove this was done correctly"

**The situation:** An auditor asks you to demonstrate that your ML experiment
reviewer agent actually read the baseline CSV before making a deployment
recommendation — and that the output hasn't been tampered with since.

**With NovaFabric:**

```bash
nova export-evidence capsules/01KRB4F7…
```

This produces a signed ZIP containing the full event trace, the inputs, the outputs,
and a cryptographic signature. The auditor can verify the signature and read the
exact chain of events.

---

## How it fits together

```
         nova capture
              │
              ▼
         [capsule]  ←── tamper-evident record of one agent run
              │
       ┌──────┼──────┐
       ▼      ▼      ▼
   validate  diff  replay
              │      │
              │    [replay capsule]
              │
         lineage import
              │
              ▼
     [lineage graph]  ←── which runs consumed which assets
```

---

## Capture approaches

You don't have to change your agent code to use NovaFabric. There are four ways to
capture:

| Approach | When to use | Example |
|---|---|---|
| `nova capture` | You control how the process starts | `nova capture python agent.py` |
| `nova api-proxy` | Agent already running as a service, or non-Python | `OLLAMA_HOST=http://localhost:9900 python agent.py` |
| `nova mcp-proxy` | Agent uses MCP tools over stdio | `nova mcp-proxy -- python mcp_server.py` |
| In-process hooks | Jupyter notebook, embedded agent | `from novafabric.capture.hooks import install_hooks` |

---

## The testbench in one sentence

The `nova-testbench` runs four real agents continuously — HPC failure analyst,
ML experiment reviewer, IoT anomaly detector, incident triage — so that the
dashboard always shows live capsules, real diffs, and a growing lineage graph.
It is the simplest proof that the whole pipeline works end to end.

```bash
make nova-loop       # run all agents in a continuous loop
make nova-dashboard  # open the live dashboard
```

---

## When does NovaFabric pay off?

The system earns its value the first time you hit one of these:

- *"Why did this agent output change?"* → `nova diff` answers in 30 seconds.
- *"Which runs do I need to re-validate after this data update?"* → lineage blast
  radius answers in 30 seconds.
- *"Can I reproduce this result from six months ago?"* → `nova replay` answers in
  minutes.
- *"Prove to the auditor what happened."* → `nova export-evidence` answers with a
  signed archive.

Without NovaFabric, each of those questions costs hours of forensic work — if it's
answerable at all.
