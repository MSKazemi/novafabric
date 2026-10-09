# Capturing a containerized run

**Status: works today.** The capsule contents below were measured on a real run
(docker 28.x, NovaFabric 0.101.0). `run.sh` and the hook-loader behaviour in
[LLM-call capture needs NovaFabric in the image](#llm-call-capture-needs-novafabric-in-the-image)
were written on 2026-10-09 on a machine with no Docker: what they cause NovaFabric
to send to Docker is tested against a stub `docker` on every test run, and the
real-daemon tests run only where a daemon is reachable.

Every other example in this tree uses the default `local` runner. This one uses
`--runner docker`, and its real subject is not the flag — it is **what ends up in
the capsule when the workload runs inside a container**, which is the question
that decides whether this is useful to a platform engineer.

The short answer, measured rather than assumed: the run itself is captured
correctly, and the *environment record describes the host, not the container*.
Details in [What is in the capsule](#what-is-in-the-capsule) below, including how
the capsule now identifies the image that ran.

## Run it

No image build, no API key, no GPU, no private registry — a stock public image
and a stdlib-only payload.

```bash
examples/docker-run/run.sh               # capsules land in examples/docker-run/capsules/
examples/docker-run/run.sh /tmp/caps     # or choose the output directory
nova validate /tmp/caps/<run_id>
```

`run.sh` runs exactly this, from the repository root:

```bash
nova capture \
  --runner docker \
  --runner-option image=python:3.12-slim \
  --runner-option "user=$(id -u):$(id -g)" \
  --runner-option workdir=/work \
  --runner-option "extra_volumes=$PWD/examples/docker-run:/work:ro" \
  -- python /work/payload.py
```

`user=` runs the workload as **you** rather than as the image's default root user,
so nothing in the container runs privileged and anything written into the
bind-mounted capsule stays owned by you on the host. The runner never adds
`--privileged`, host namespaces or the Docker socket.

**Without Docker,** `run.sh` prints a `skip:` line and exits 0 — both when there is
no `docker` binary and when `docker info` cannot reach a daemon. Calling
`nova capture --runner docker` directly with no `docker` binary does *not* skip: it
prints `Workload never started: [Errno 2] No such file or directory: 'docker'`,
exits 127, and still writes a `status: failure` capsule recording that (measured).
With a binary but no daemon, `docker run` itself exits 125, which the runner
reports as a setup failure (from the runner's source; not measured here).

> **`--runner-option` takes strings, and structured options are comma-separated.**
> `extra_volumes=a:b,c:d` is two mounts. Before v0.102.0 the CLI accepted these
> options and then silently discarded them — a container simply did not get the
> mount you asked for, with no error. Fixed; if you are on an older version, pass
> them from a config file instead.

## What is in the capsule

Captured from a real run of the command above, before `user=` was added
(`python:3.12-slim`, docker 28.x, NovaFabric 0.101.0). **Read this as a report of
what is true today, not a specification.**

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
different. `host.runner` (next section) is what says the run was containerized.

### What identifies the container run

**Experimental, unreleased (ADR-0307, issue #157).** `capsule.yaml` records the
runner and the image the Docker daemon resolved the tag to:

```yaml
host:
  runner:
    name: docker
    image:
      reference: python:3.12-slim                 # what you passed
      image_id: sha256:<64 hex>                   # what the daemon resolved it to
      repo_digests:
        - python@sha256:<64 hex>                  # the pullable, pinned form
      resolved_by: docker-image-inspect
```

The runner asks `docker image inspect` before and after `docker run`; the tag
itself is never trusted. If the digest cannot be resolved, `image` carries
`unresolved_reason` instead (the daemon's own error, or "the tag resolved to …
before the run and to … after it" when the tag was re-pointed while the workload
ran). A capsule of this run and a capsule of `python payload.py` on the host now
differ in `host.runner`.

How this was checked: against a stub `docker` that answers `image inspect` the way
the daemon does (`tests/test_example_docker_run.py::
test_run_sh_capsule_records_the_runner_and_the_resolved_digest`). It has not yet
been measured on a real daemon; the capsule excerpts elsewhere in this README
predate it.

### LLM-call capture needs NovaFabric in the image

Since v0.102.0 (defect B3) the Docker runner writes its hook loader into the
capsule and puts it on the container's `PYTHONPATH`, so wire-level capture can
fire inside the container. It can only fire if the **image** has NovaFabric
installed — the runner's own docstring makes that the image's responsibility.
`python:3.12-slim` does not, so for this example the loader runs, fails to import
`novafabric`, and writes `[novafabric] hook install failed: No module named
'novafabric'` to the capsule's `outputs/stderr.txt`.

The payload makes no model calls, so this example loses nothing. A containerized
agent that does make them needs an image built with `pip install novafabric`;
on a stock image its `model-calls.jsonl` comes back empty while the run still
reports `status: success`. Check `outputs/stderr.txt` for that line.

### Which environment crosses into the container

Not the submitting shell's. The runner forwards a **default-deny allowlist**
(ADR-0270): NovaFabric's own `NOVAFABRIC_*` variables, the `PYTHONPATH` it sets
itself, and anything passed explicitly with `--runner-option extra_env=…`.
Provider keys, tokens and everything else in your shell stay on the host. The
test suite pins this against the exact `docker run` argv: a variable set in the
calling environment never appears in it.

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
| `run.sh` | the documented command; skips with exit 0 when Docker is unavailable |
| `payload.py` | stdlib-only workload that prints what interpreter and host it sees |
| `README.md` | this file |

The regression test is `tests/test_example_docker_run.py`.
