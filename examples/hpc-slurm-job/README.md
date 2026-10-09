# Capturing a Slurm batch job

**Status: works today.** `job.sbatch` was verified on a real single-node Slurm
23.11 cluster (Ubuntu 24.04, 8 vCPU) on 2026-08-29, not only in CI. `submit.sh`
was added later as a thin wrapper around the same `sbatch job.sbatch`; it has
been exercised against a stub `sbatch` in the test suite, not a real scheduler.

The README's first line promises capture of "a script, an agent, a model run, an
HPC training job". Every other example in this tree is laptop-shaped. This one
shows the intended pattern for a batch job — and, honestly, what the capsule does
and does not know about the job that produced it.

## The pattern

There is no Slurm plugin and no scheduler integration to install. `nova capture`
wraps the payload **inside** the batch script:

```bash
nova capture --output-dir "${CAPSULE_DIR}" --environment production -- \
    python3 "${SCRIPT_DIR}/payload.py"
```

Slurm schedules the job; NovaFabric captures what the job did. Neither needs to
know about the other, which is why this works on any scheduler.

```bash
cd examples/hpc-slurm-job
sbatch job.sbatch        # or ./submit.sh, which does the same
```

`./submit.sh` exists for the machine that has no Slurm: when `sbatch` is not on
`PATH` it prints a `skip:` message pointing at the no-scheduler path below and
exits 0, rather than failing on a laptop, a CI runner or a fresh clone.

### Two things that will bite you, both measured on a real cluster

1. **`dirname "$0"` does not point at your files.** Slurm copies the batch script
   to a per-job spool directory on the compute node, so inside the job it resolves
   to something like `/var/spool/slurmd/job00001`. The first run of this example
   failed exactly that way. Use `SLURM_SUBMIT_DIR`, which `job.sbatch` does.
2. **Write capsules to a shared filesystem.** `job.sbatch` passes an explicit
   `--output-dir` under `SLURM_SUBMIT_DIR`. A capsule written to a compute node's
   `/tmp` is gone by the time you look for it, and `NOVAFABRIC_HOME` is not enough
   on its own because a config file can override the environment variable.

`--experiment` is deliberately **not** used here. It is A/B attribution
(ADR-0116) and requires `--variant` and `--variant-source` alongside it; it is not
a label for the job, and using it as one fails at the CLI.

## Without a scheduler

The important constraint: this example runs on a machine with no Slurm at all.

```bash
python3 payload.py                      # the payload alone
nova capture -- python3 payload.py      # the same capture, no scheduler
./job.sbatch                            # the batch script as a plain shell script
```

`payload.py` reads `SLURM_*` from the environment and reports `scheduler = none
(running locally)` when they are absent. `job.sbatch` falls back to its own
directory when `SLURM_SUBMIT_DIR` is unset, and writes capsules to
`./novafabric-capsules/` (override with `NOVAFABRIC_CAPSULE_OUT`). Check one with:

```bash
nova validate novafabric-capsules/<run_id>
```

The accompanying test (`tests/test_example_hpc_slurm_job.py`) runs all three lines
above with no scheduler and checks the capsule with `nova validate`; checks that
`submit.sh` skips with exit 0 when `sbatch` is absent and calls `sbatch job.sbatch`
from the example directory when it is present (a stub, not a real scheduler);
and checks that every flag `job.sbatch` passes to `nova capture` is one the CLI
declares.

## What is in the capsule — and what is not

From a verified single-node Slurm run (`sbatch job.sbatch`, job 2):

**Captured, and correct:** the command, exit code, status, duration, working
directory, the full stdout/stderr of the job, the environment lock of the compute
node it ran on, and a `redaction-proof.json`. `nova validate` accepts it.

### What the capsule records about the job

**Experimental, since v0.105.0 (ADR-0307, issue #157).** The verified run above predates
this; it is checked in the test suite with the Slurm variables set by hand, not yet
on a cluster. When `nova capture` runs inside a Slurm job, `capsule.yaml` carries:

```yaml
host:
  runner:
    name: local                      # nova capture ran the payload directly
  slurm:
    job_id: "2"                      # SLURM_JOB_ID
    partition: debug                 # SLURM_JOB_PARTITION, when set
    cluster: mycluster               # SLURM_CLUSTER_NAME, when set
    node_count: 1                    # SLURM_JOB_NUM_NODES, when set
    node_list_hash: sha256:<16 hex>  # SLURM_JOB_NODELIST, hashed
    source: environment
```

Array jobs add `array_job_id` and `array_task_id`. Those variables are the whole
list: NovaFabric reads them by name, never `SLURM_*` by prefix, so the account,
the submit directory and anything else your site exports stay out. A value Slurm
did not set is left out, never guessed.

**Node names are hashed, not recorded.** They are hostnames, and the manifest
carries no hostname (`host.hostname_redacted: true`). To check whether a job ran on
`node[01-04]`, hash that string and compare. The node name still reaches the
capsule in `outputs/stdout.txt`, and only there, because `payload.py` prints it.
`test_the_capsule_records_the_slurm_context` pins both halves.

So a capsule of this batch job and a capsule of the same script run on a login
node are no longer indistinguishable: only the first has `host.slurm`. Printing
the scheduler variables from the workload, as `payload.py` does, is still how the
node name and anything outside the list above get into sealed evidence.

## What this does not prove

- **It does not prove multi-node capture.** One node, one task. Capturing a job
  that spans nodes — one capsule per rank, or one per job — is an open design
  question, not a solved one.
- **It does not exercise the collector.** Nothing is uploaded; the capsule stays
  on the shared filesystem. The HPC collector tier in
  [`deploy/hpc/`](../../deploy/hpc/) (Slurm prolog/epilog + NATS) is **planned**
  and is a different thing from this example — do not read one as evidence of the
  other.
- **It does not use the `--runner slurm` backend.** That submits *through*
  NovaFabric; this example captures *inside* a job you submitted yourself, which
  is the pattern that fits existing cluster workflows.
- **It says nothing about GPUs, MPI, or job arrays.**

## Files

| File | What it is |
|---|---|
| `job.sbatch` | the batch script; the capture pattern lives here |
| `submit.sh` | `sbatch job.sbatch` from the right directory, or a `skip:` and exit 0 with no Slurm |
| `payload.py` | stdlib-only stand-in for a training script |
| `README.md` | this file |
