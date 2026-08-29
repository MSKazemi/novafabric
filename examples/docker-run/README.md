# Capturing a containerized run

**Status: works today.**

Every other example in this tree uses the default `local` runner. This one uses
`--runner docker`, and its real subject is not the flag — it is **what ends up in
the capsule when the workload runs inside a container**, which is the question
that decides whether this is useful to a platform engineer.

The short answer, measured rather than assumed: the run itself is captured
correctly, and the *environment record describes the host, not the container*.
Details in [What is in the capsule](#what-is-in-the-capsule) below, including two
things that are missing and are tracked as their own issue.

## Run it

No image build, no API key, no GPU, no private registry — a stock public image
and a stdlib-only payload.

```bash
nova capture \
  --runner docker \
  --runner-option image=python:3.12-slim \
  --runner-option workdir=/work \
  --runner-option "extra_volumes=$PWD/examples/docker-run:/work:ro" \
  python /work/payload.py
```

Without Docker, `nova capture --runner docker` reports that the daemon is
unreachable and exits non-zero rather than pretending to run; the accompanying
test skips cleanly instead.

> **`--runner-option` takes strings, and structured options are comma-separated.**
> `extra_volumes=a:b,c:d` is two mounts. Before v0.102.0 the CLI accepted these
> options and then silently discarded them — a container simply did not get the
> mount you asked for, with no error. Fixed; if you are on an older version, pass
> them from a config file instead.

## What is in the capsule

Captured from a real run of the command above (`python:3.12-slim`, docker 28.x,
NovaFabric 0.101.0). **Read this as a report of what is true today, not a
specification.**

### What proves the workload ran in the container

`outputs/stdout.txt` — the payload prints the interpreter and hostname it sees:

```
payload: python   = 3.12.14          <- the image's Python
payload: hostname = b5de52d9c131     <- the container ID
payload: capsule  = /novafabric/capsule
```

The capsule directory is bind-mounted into the container and rewritten to a
container-relative path, so the workload writes its evidence straight into the
capsule you get back on the host.

### What describes the host instead of the container

`env.lock` and `capsule.yaml` are produced by the NovaFabric process, which runs
on the **host**. So for the run above:

| Field | Value recorded | What it actually describes |
|---|---|---|
| `capsule.yaml host.python` | `3.14.6` | the host's Python — the container ran 3.12.14 |
| `env.lock python.executable_path` | the host venv | the host |
| `env.lock python.installed_packages` | the host's packages | the host |
| `capsule.yaml host.cpu_count` / `memory_bytes` | the host's | the host |

This is not wrong so much as **narrower than it looks**: the environment lock is
an honest record of the machine that performed the capture, and for a `local` run
that is also the machine that ran the workload. For a container run the two are
different, and the capsule does not currently say so.

### What is missing

Two things a reader would reasonably expect and will not find:

1. **The image reference and digest are recorded nowhere.** Not the tag, not the
   `sha256:` digest. You cannot tell from the capsule which image produced it.
2. **The runner is not recorded either.** Nothing in the capsule says the run was
   containerized.

Together these mean a capsule of this run and a capsule of `python payload.py` on
the host differ only in their outputs — there is no field that distinguishes them.
For an evidence format, that is worth fixing, and it is deliberately **not** fixed
in this example. See `examples/hpc-slurm-job/README.md`, which finds the same gap
for the Slurm runner — one gap, two runners.

### Redaction still applies

`redaction-proof.json` is written for a container run exactly as for a local one;
the secret scanner runs host-side over the captured streams, so it does not care
where the process ran.

## What this example does not show

- **Multi-container or Compose workloads.** One container, one command.
- **Private registries, credentials, or image pulls.** The image is public and
  pulled by Docker itself, outside NovaFabric's view.
- **Resource limits, GPU passthrough, or custom networks.** `--runner-option
  network=` exists; this example uses the default bridge.
- **The collector.** Nothing is uploaded; the capsule stays on disk.
- **Rootless Docker or Podman.** `DockerRunner(docker_bin=...)` is override-friendly
  but untested here.

## Files

| File | What it is |
|---|---|
| `payload.py` | stdlib-only workload that prints what interpreter and host it sees |
| `README.md` | this file |

The regression test is `tests/test_example_docker_run.py`.
