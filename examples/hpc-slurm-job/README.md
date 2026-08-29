# Capturing a Slurm batch job

**Status: works today.** Verified on a real single-node Slurm 23.11 cluster
(Ubuntu 24.04, 8 vCPU) on 2026-08-29, not only in CI.

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
sbatch job.sbatch
```

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
directory when `SLURM_SUBMIT_DIR` is unset. The accompanying test
(`tests/test_example_hpc_slurm_job.py`) exercises the no-scheduler path, and skips
the `sbatch` path cleanly when no scheduler is present.

## What is in the capsule — and what is not

From a verified single-node Slurm run (`sbatch job.sbatch`, job 2):

**Captured, and correct:** the command, exit code, status, duration, working
directory, the full stdout/stderr of the job, the environment lock of the compute
node it ran on, and a `redaction-proof.json`. `nova validate` accepts it.

**Not captured: any Slurm context at all.** The capsule records no job ID, no node
name, no cluster name, no partition, no allocation. Grepping the whole capsule for
"slurm" matches exactly one file — `outputs/stdout.txt` — and only because
`payload.py` deliberately prints those variables itself.

The consequence is worth stating plainly: **a capsule of this batch job and a
capsule of the same script run on a login node are indistinguishable.** For a
format whose purpose is to prove what a run did, "which job was this?" is a
question it currently cannot answer. This is the same gap the
[`docker-run/`](../docker-run/) example finds for containers, where the image
reference and digest are likewise absent — one gap, two runners.

Until then, the workable pattern is the one `payload.py` uses: print the
scheduler variables from inside the workload so they land in the captured stdout,
where they are at least sealed with everything else.

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
| `payload.py` | stdlib-only stand-in for a training script |
| `README.md` | this file |
