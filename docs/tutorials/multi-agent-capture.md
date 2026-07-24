# Capturing multi-agent systems

> **Who this is for:** engineers building systems where multiple AI agents
> communicate with each other — orchestrators calling sub-agents, pipelines
> of specialist agents, or fully autonomous agent meshes.

---

## The core question: same process or separate processes?

How NovaFabric captures a multi-agent system depends entirely on whether the
agents share one Python process or run as separate processes.

---

## Case 1: All agents in one process

LangGraph multi-agent graphs, AutoGen, CrewAI, and most agentic frameworks run all
agents inside a single Python process. One `nova capture` wraps the whole thing:

```bash
nova capture python orchestrator.py
```

Because NovaFabric hooks into `requests` and `aiohttp` at the process level, every
LLM call from every agent — orchestrator, analyst, writer, reviewer — goes through
the same hook. All of them land in one capsule, in chronological order:

```
model-calls.jsonl:
  09:00:01  system: "you are the coordinator..."   → "assign analysis to analyst"
  09:00:03  system: "you are the data analyst..."  → "here are the findings"
  09:00:05  system: "you are the report writer..."  → "here is the draft"
  09:00:07  system: "you are the coordinator..."   → "approved, publish"
```

You can tell which agent made which call by looking at the system prompt in each
event — each agent has a different one.

**What you get:** one capsule, the full back-and-forth conversation across all
agents, one `nova diff`, one `nova replay`. Zero code changes required.

---

## Case 2: Agents in separate processes

When Agent B is a service and Agent A calls it over HTTP:

```
[Agent A process]  →  HTTP call  →  [Agent B process]
  nova capture              ?              no capture
  wraps A                                 on B's side
```

`nova capture` on Agent A records everything on A's side — the outgoing HTTP call
and the response. But Agent B's internal LLM calls, tool calls, and reasoning steps
are invisible. You only see the boundary, not what happened inside B.

### Fix: wrap each process separately

```bash
# Terminal 1 — Agent B (the sub-agent / service)
nova capture python agent_b.py --output-dir capsules/

# Terminal 2 — Agent A (the orchestrator)
nova capture python agent_a.py --output-dir capsules/
```

Each gets its own capsule with its own full trace. You now have visibility inside
both agents.

**The remaining problem:** the two capsules are unlinked. You know what A did and
what B did, but the database doesn't know that A triggered B.

### Fix: `nova api-proxy` for shared LLM visibility

If both agents talk to the same LLM, route them both through the proxy:

```bash
# Start the proxy once
nova api-proxy --port 9900 --upstream http://localhost:11434

# Agent B — points to proxy
OLLAMA_HOST=http://localhost:9900 nova capture python agent_b.py

# Agent A — also points to proxy
OLLAMA_HOST=http://localhost:9900 nova capture python agent_a.py
```

Both agents' LLM calls flow through the same proxy. Both are captured. You get
two capsules, fully traced.

---

## Linking capsules: parent/child (primitive shipped — ADR-0039)

The missing piece for multi-process agents is **linking** — knowing that capsule B
was created because capsule A triggered it.

The parent/child primitive itself **is implemented** (ADR-0039): capsules carry
`parent_run_id` and `capsule_role`, and the env-var contract
(`capture/env_contract.py`) reads `NOVAFABRIC_PARENT_RUN_ID`. It is wired today for
**distributed-job WORKER capsules** (the DRIVER/WORKER model of `cluster-scale.md`).
What is **not yet wired** is auto-population from the plain `nova capture` path: the
capture orchestrator does not read `NOVAFABRIC_PARENT_RUN_ID`, so the subprocess
pattern below sets the linkage intent but the emitted capsule does not yet record it.

The intended shape uses an environment variable:

```python
# Inside the orchestrator (Agent A), when spawning Agent B:
import subprocess, os

subprocess.run(
    ["nova", "capture", "--output-dir", "capsules/", "python", "agent_b.py"],
    env={
        **os.environ,
        "NOVAFABRIC_PARENT_RUN_ID": current_run_id   # links B to A
    }
)
```

Agent B's capsule records its parent run ID. The lineage graph shows the full tree:

```
orchestrator run (A)
    ├── analyst run (B)       triggered by A
    ├── writer run (C)        triggered by A
    └── reviewer run (D)      triggered by A
```

And you can query: "show me everything that ran as part of orchestrator run A" —
across all processes, all machines, all timing.

The schema fields and env-var contract are shipped (ADR-0039); wiring them into the
plain `nova capture` orchestrator is the remaining step, pending empirical validation
on real multi-agent workloads.

---

## Case 3: MCP-based multi-agent

When the orchestrator calls sub-agents as MCP tools (served over stdio), use
`nova mcp-proxy` to capture the tool layer:

```bash
# Wrap the MCP server — captures every tool call and response
nova mcp-proxy -- python mcp_agent_server.py

# The orchestrator calls the proxy thinking it's the real MCP server
nova capture python orchestrator.py
```

Every tool call (agent-to-agent message) is recorded in `tool-calls.jsonl` inside
the orchestrator's capsule, alongside the orchestrator's own LLM calls.

---

## What works today vs what's coming

| Topology | Capture today | What you get |
|---|---|---|
| Multiple agents, one process | `nova capture python orchestrator.py` | One capsule, full trace of all agents |
| Multiple agents, separate processes | `nova capture` on each separately | Separate capsules, unlinked |
| Shared LLM, separate processes | `nova api-proxy` + `nova capture` on each | Separate capsules, both fully traced, unlinked |
| MCP sub-agents | `nova mcp-proxy` + `nova capture` | Tool calls captured inside orchestrator capsule |
| Linked parent/child (cross-process) | ADR-0039 — primitive shipped; `nova capture` auto-linking not yet wired | Full provenance tree across processes |

---

## The honest limitation

The hard multi-agent problem — agents running on different machines, different
clusters, spawned dynamically at runtime — is where the plain `nova capture` path
still has a seam. You get per-agent capsules, and the parent/child primitive exists
(ADR-0039), but the `nova capture` orchestrator does not yet auto-stitch them into a
single provenance tree.

Wiring that stitching into `nova capture` is the remaining step (ADR-0039). The
`nova-testbench` was built to generate real multi-step, multi-agent workloads so the
parent/child path can be validated against actual traffic before it is enabled by
default. See `docs/tutorials/cluster-scale.md` for
the architecture at large scale.
