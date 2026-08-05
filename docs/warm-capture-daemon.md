# Warm capture daemon

> **Status: experimental.** Linux only. Opt-in — with no daemon running,
> `nova capture` behaves exactly as it always has. Introduced by
> [ADR-0092](./decisions.md); extends
> [ADR-0020](./decisions.md);
> realizes the "resident emitter" idea (SI-2).

This is a focused guide to one of NovaFabric's more intricate pieces. Read it if
you run many captures on one node (a fleet, an HPC job array, a CI farm) and want
to remove the per-run startup cost — or if you're extending the daemon.

---

## 1. The problem in one paragraph

`nova capture <cmd>` is a fresh Python process per run. Before it can do anything
it pays a **cold-start**: the Python interpreter plus importing the `novafabric`
package. On a warm filesystem that is a few hundred milliseconds; on a cold
filesystem / first-run-per-node it was measured at **~3 seconds** (SPK-COL-2).
The steady-state per-event capture cost is already tiny (+0.36 %), so the thing
worth removing at fleet scale is **process startup paid once per run**.

This is exactly how every mature edge-telemetry agent is built — the
OpenTelemetry **agent** pattern (one collector per node), the Datadog Agent,
Vector, Fluent Bit — a long-lived node-resident process, never a fresh process
per workload.

## 2. The idea: pay the import once, fork per run

```
┌──────────────────────────────────────────────────────────────────────────┐
│  nova daemon start   (one warm process per node)                           │
│  • imports novafabric ONCE  ── pays the cold-start a single time           │
│  • listens on  $NOVAFABRIC_HOME/run/capture.sock  (AF_UNIX, 0600)          │
└───────────────┬────────────────────────────────────────────────────────────┘
                │  per run:
   novacap  ───►│  1. send {argv, cwd, env}   2. pass stdin/stdout/stderr (SCM_RIGHTS)
 (thin client)  │  3. parent os.fork()  ──►  worker (copy-on-write WARM, ~0 import)
                │                              • dup2 your stdio onto the workload
                │                              • run CaptureOrchestrator  (UNCHANGED)
                │                              • write capsule + seal + lineage
                │◄─────────────────────────── exit code
   exit(code) ◄─┘
```

Each run is served by a **fork** of the already-warm parent. The fork inherits
all imported modules via copy-on-write, so the worker pays essentially **zero**
import cost — and it is a full separate process, so it keeps NovaFabric's
invariant **"one run = one process, one capsule = one writer."** The worker runs
the *existing* `CaptureOrchestrator` unchanged, which is why a capsule produced
through the daemon is **structurally identical** to one from a plain
`nova capture` (verified by an automated fidelity test).

> **Why fork and not threads?** Threads would share one process, breaking the
> one-writer invariant and forcing a risky rewrite of the capture core's
> module-global state. A forked run is also isolated: if it crashes or runs out
> of memory, only that run dies — not the daemon and every other run.

## 3. Using it

### Start the daemon

```bash
nova daemon start                 # foreground (recommended under a supervisor)
nova daemon start --max-concurrency 128
nova daemon status                # "running" / "not running"
nova daemon stop                  # graceful SIGTERM
```

Run `nova daemon start` under a process supervisor — systemd, a Slurm Prolog, or
a Kubernetes DaemonSet — so it stays alive for the node's lifetime.

### Capture through it

Use the thin client **`novacap`** as your per-run entry point:

```bash
novacap python agent.py
```

`novacap` imports **no** NovaFabric code, so it starts in tens of milliseconds.
It forwards your command, working directory, and environment to the daemon and
passes your terminal through, so the workload behaves exactly as if you'd run it
directly. **If no daemon is reachable, `novacap` falls back to
`nova capture --no-daemon` automatically — it never blocks your workload.**

For fleet use, make `novacap` the per-run wrapper (e.g. inside your Slurm
`srun` line). That is where the cold-start saving is actually realized.

### Or let `nova capture` delegate

`nova capture` gained `--daemon/--no-daemon` (default **auto**):

```bash
nova capture python agent.py          # delegates to the daemon if one is running
nova capture --no-daemon python agent.py   # always in-process
```

A **plain** capture delegates; any capture that uses `--runner`,
`--runner-option`, `--timeout`, `--asset`, `--mark-provenance`, or `--output-dir`
runs **in-process** so those flags are honored (the thin client only forwards
argv/cwd/env). Note that invoking `nova capture` already pays its own import, so
the full cold-start win comes from invoking `novacap` directly.

## 4. What it does — and does not — speed up (honest boundary)

There are **two** startup costs in a capture:

| # | Cost | Removed by the daemon? |
|---|------|------------------------|
| **#1** | the **orchestrator** process importing `novafabric` | ✅ yes — paid once at `daemon start` |
| **#2** | the **workload** process importing `novafabric` via `sitecustomize` to install the in-process wire hooks (only for nova-instrumented **Python** agents) | ❌ no — runs in the agent's own process |

Measured on a developer laptop (warm filesystem):

| workload | direct `nova capture` | `novacap` (warm daemon) | reduction |
|---|---|---|---|
| `/bin/true` (no workload-side nova import) | 593.9 ms | 209.6 ms | **−64.7 %** |
| `python -c pass` (nova-instrumented agent) | ~1.62 s | ~1.43 s | ~−9–12 % |

So the daemon delivers a large win on the orchestrator cold-start (#1) — and the
remaining cost for a Python agent is its own hook-install import (#2).
On cold-filesystem / first-run-per-node fleets, #1 is seconds, so the absolute
saving there is much larger than the warm-fs laptop numbers above.

**`--fast-emit` attacks cost #2 (shipped, v0.54.0).** The default hook installer
imports *every* present SDK (`openai`, `mcp`, `requests`, …) at the workload's
startup purely to patch it — ~717 ms for `openai`, ~340 ms for `mcp`, paid even
when the workload never calls them. `nova capture --fast-emit python agent.py`
patches each SDK only if/when the workload itself imports it, so unused SDKs are
never imported by capture:

| workload | eager (default) | `--fast-emit` | reduction |
|---|---|---|---|
| pure compute (imports no instrumented SDK) | ~2068 ms | ~464 ms | **−78 %** |
| `import openai` (one SDK, used) | ~2223 ms | ~1509 ms | **−32 %** |

The saving scales inversely with how many SDKs the workload uses; fidelity is
unchanged. `--fast-emit` and the daemon are complementary — `--fast-emit` runs
in-process (it is not delegated to the daemon), but a daemon-run orchestrator
with `fast_emit=True` gets both wins. See [§3](#3-using-it) below for usage.

## 5. Safety and security

- **Never blocks the workload.** Socket missing, unreachable, version-mismatched,
  or at the concurrency cap → the client falls back to a direct in-process run. A
  daemon crash does not kill in-flight runs (they are independent processes); only
  *new* runs fall back.
- **Cancellation, no orphans.** Each run is its own process group. Ctrl-C on
  `novacap` (or the client disconnecting) terminates the run's whole process group
  (SIGTERM, then SIGKILL after a short grace).
- **Local only.** The socket is an `AF_UNIX` socket under
  `$NOVAFABRIC_HOME/run/` (directory `0700`, socket `0600`, owned by the agent
  UID). Connections from other UIDs are rejected (`SO_PEERCRED`). **There is no
  network listener.** No root is required.
- **No new dependencies.** Pure Python standard library.

## 6. Configuration

| Setting | How | Default |
|---|---|---|
| Socket path | `NOVAFABRIC_CAPTURE_SOCKET` | `$NOVAFABRIC_HOME/run/capture.sock` |
| Data root | `NOVAFABRIC_HOME` | `~/.novafabric` |
| Max concurrent runs | `nova daemon start --max-concurrency N` | 64 |

## 7. Troubleshooting

- **`novacap` is no faster than `nova capture`.** Your workload is a
  nova-instrumented Python process, so cost #2 dominates — that's expected (see
  §4). Try a non-Python workload to see the #1 saving, or add `--fast-emit`
  (v0.54.0) to cut the unused-SDK part of #2.
- **`nova daemon status` says "not running" but a socket file exists.** A stale
  socket from a crashed daemon. `nova daemon stop` removes a stale pidfile;
  delete the socket file if needed and `nova daemon start` again.
- **Permission denied connecting.** The socket is owned by the UID that started
  the daemon; run `novacap` as the same user.

## 8. Roadmap (what comes next)

This is **slice A** of the resident-emitter work. Status:

- **Slice B — shipped (v0.54.0):** `nova capture --fast-emit` defers hook install
  so unused SDKs are never imported at startup (attacks cost #2). See [§4](#4-what-it-does--and-does-not--speed-up-honest-boundary).
- **Slice C — C0 experimental:** `nova capture --emit-spool` writes run-boundary
  EventEnvelope v1 records to a local spool (`$NOVAFABRIC_SPOOL_DIR`), and the
  resident Go binary `novafabric-spool-forwarder` drains it and publishes to a NATS
  JetStream stream on `<prefix>.<run_id>`. Signing happens at the **hub**, not the
  edge — compute nodes hold no NovaSeal keystore (hub-sign default; preserves the HPC
  air-gap). Exactly-once in steady state; restart-based no-loss on publish failure.
  Remaining: hub-sign + offline verify (C1), the OTel-Arrow gateway hop (C2,
  `deploy/collector-arrow/`), and Slurm/K8s rollout (C3).
- **Deployment artifacts** — a Kubernetes DaemonSet manifest and a Slurm
  Prolog/Epilog rollout.

See [ADR-0092](./decisions.md) for the full design and
rationale, and the
[cluster-scale architecture](./architecture.md) for where
the edge emitter sits in the larger picture.
