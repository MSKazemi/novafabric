# NovaFabric Operator Guide

This document is for HPC system administrators and platform engineers who need
to deploy NovaFabric on shared infrastructure — from a single workstation to a
multi-node SLURM cluster. It covers installation, runner configuration, wire-level
capture setup, and site-specific URL registry customization.

For developer onboarding (contributing code, running tests), see
[CONTRIBUTING.md](../CONTRIBUTING.md). For CLI usage reference, see
[docs/cli-reference.md](cli-reference.md).

---

## Table of contents

1. [Deployment model overview](#1-deployment-model-overview)
2. [Prerequisites](#2-prerequisites)
3. [Deployment scenarios](#3-deployment-scenarios)
   - 3.1 [Laptop or single workstation (LocalRunner)](#31-laptop-or-single-workstation-localrunner)
   - 3.2 [Docker or OCI container (DockerRunner)](#32-docker-or-oci-container-dockerrunner)
   - 3.3 [SLURM cluster (SlurmRunner)](#33-slurm-cluster-slurmrunner)
   - 3.4 [Kubernetes (KubernetesRunner)](#34-kubernetes-kubernetesrunner)
4. [URL registry configuration](#4-url-registry-configuration)
5. [Troubleshooting](#5-troubleshooting)
6. [What is not supported yet](#6-what-is-not-supported-yet)

---

## 1. Deployment model overview

NovaFabric is **local-first**: there is no server, no agent daemon, and no
persistent network service required to capture a run. Capture happens in-process
inside the workload's own Python interpreter.

The mechanism is a `sitecustomize.py` hook loader. When `nova capture` launches a
workload, it materializes this file to a directory that is prepended to
`PYTHONPATH`. Python's import machinery auto-loads `sitecustomize` at interpreter
startup, before any user code runs. The hooks patch `httpx`, `requests`,
`aiohttp`, and `urllib3` transports to intercept outbound HTTP calls and write
structured records to a capsule directory.

Every runner — local subprocess, Docker container, SLURM batch job, Kubernetes
Job — uses this same mechanism. The runner's only job is to ensure:

- `NOVAFABRIC_CAPSULE_DIR` points at a writable directory.
- That directory (or another directory on `PYTHONPATH`) contains `sitecustomize.py`.
- All capsule artifacts from the workload are present on the local filesystem
  before `nova capture` returns.

The capsule directory holds the complete record of a run: `capsule.yaml` (metadata),
`model-calls.jsonl` (wire-level AI traffic), and `outputs/` (stdout, stderr). No
data leaves the node unless you explicitly push a capsule somewhere.

---

## 2. Prerequisites

### Python version

Python 3.12 or later is required on every node that runs `nova capture` or any
NovaFabric CLI command. This applies to both the submit node and compute nodes
on SLURM clusters.

### Package installation

Install with `pip` or `uv`. Both work. `uv` is recommended for reproducible
environments:

```bash
# pip
pip install novafabric

# uv (recommended for shared or HPC venvs)
uv pip install novafabric
```

The optional `serve` extra adds the experimental local dashboard (see section 6):

```bash
pip install "novafabric[serve]"
```

### What compute nodes need

For any runner that executes on a remote node (SLURM, Kubernetes):

- Python 3.12+ in the same virtual environment (or an identically installed venv
  at the same path) that the submit node uses.
- NovaFabric installed in that venv (`import novafabric` must succeed on the
  compute node).
- Network access to the inference endpoints you want to capture (or none, if the
  workload is expected to fail the connection — capture still records the attempt).

NovaFabric does **not** require internet access for capture, validation, replay,
diff, or local lineage queries. The only outbound traffic is the workload's own AI
API calls, and only if the workload makes them.

### Scheduler tools

| Runner | Required binaries |
|--------|------------------|
| `local` | none beyond Python |
| `docker` | `docker` (daemon must be reachable) |
| `slurm` | `sbatch`, `scontrol`, `sacct` on `PATH` |
| `kubernetes` | `kubectl` configured with a reachable cluster |

---

## 3. Deployment scenarios

### 3.1 Laptop or single workstation (LocalRunner)

**Works today.** This is the default runner and requires no configuration beyond
installing the package.

```bash
nova capture python my_agent.py
```

`nova capture` launches `my_agent.py` as a subprocess, injects
`sitecustomize.py` into a temporary directory on `PYTHONPATH`, and writes the
capsule to `$NOVAFABRIC_HOME/capsules/<run-id>/` (override with `NOVAFABRIC_CAPSULE_DIR`).

To inspect a run:

```bash
nova validate <run-id>
nova show <run-id>
```

No shared filesystem, no scheduler, no network service needed. The capsule is
self-contained on disk.

**Explicit runner flag** (same as default):

```bash
nova capture --runner local python my_agent.py
```

**Timeout** (default 600 s):

```bash
nova capture --runner local --timeout 120 python my_agent.py
```

### 3.2 Docker or OCI container (DockerRunner)

**Works today.** The workload runs inside a Docker container. The capsule
directory is mounted as a volume so the orchestrator can read artifacts after
the container exits.

**Requirements:**

- `docker` binary on `PATH` on the submit node.
- Docker daemon reachable (check with `docker info`).
- A container image that has NovaFabric installed. The runner does not pick a
  default image. Build your own:

```dockerfile
FROM python:3.12-slim
RUN pip install novafabric
COPY my_agent.py /app/my_agent.py
WORKDIR /app
```

**Basic invocation:**

```bash
nova capture \
  --runner docker \
  --runner-option image=myorg/agent-runtime:abc1234 \
  python my_agent.py
```

**All `runner_options` for DockerRunner:**

| Key | Required | Description |
|-----|----------|-------------|
| `image` | yes | Fully-qualified image reference |
| `network` | no | `--network` value (default: Docker default / bridge) |
| `workdir` | no | Container working directory |
| `user` | no | `--user` value (e.g. `1000:1000`); recommended to avoid root-owned capsule files |
| `extra_volumes` | no | List of `host:container[:opts]` mount strings |
| `extra_env` | no | Dict of additional env vars for the container |

**Project-level defaults** in `.novafabric/runners.yaml`:

```yaml
schema_version: "0.1.0"
default_runner: docker
runners:
  docker:
    image: myorg/agent-runtime:latest
    user: "1000:1000"
    network: bridge
```

**Security posture:** The DockerRunner never passes `--privileged`, does not
mount the Docker socket, and does not enable host namespaces (`hostNetwork`,
`hostPID`, `hostIPC`). These are hard restrictions in the runner code.

The runner only forwards `NOVAFABRIC_*` environment variables and any keys
specified in `extra_env` into the container. It does not pass through the
full host environment, which would risk leaking credentials into the container
at runtime.

### 3.3 SLURM cluster (SlurmRunner)

**Works today.** This section is the most detailed because SLURM introduces
constraints that do not exist for local or Docker runners.

#### Overview

The SlurmRunner submits the workload via `sbatch --wrap`, polls for completion
using `scontrol show job` (primary) with `sacct` as a fallback, and reads
stdout/stderr from per-job output files. Capsule artifacts accumulate on a
shared filesystem, then the orchestrator reads them on the submit node after
the job finishes.

Three failure modes were caught and fixed during live-cluster validation
(v0.6.10, v0.6.11, v0.6.12). This section reflects that validated behavior.

#### Shared filesystem requirement

This is the most important operational constraint for SLURM deployments:

**The capsule directory must be on a filesystem shared between the submit node
and every compute node SLURM might allocate.**

If the capsule directory is on node-local storage (e.g. `/tmp` or any path
that is not the cluster's shared scratch/home), the job will fail the moment
the scheduler places it on any node other than the submit node. SLURM gives no
guarantee about which node a job runs on.

Acceptable shared filesystems: NFS, Lustre, GPFS, BeeGFS, any POSIX-compatible
network filesystem that is mounted at the same path on all nodes.

Set the shared base path via the environment variable:

```bash
export NOVAFABRIC_SLURM_SHARED_DIR=/lustre/scratch/myproject
```

When this variable is set, the live integration tests also use it. When unset,
tests fall back to node-local `/tmp`, which only works on a single-node cluster.

#### Python environment on compute nodes

The compute nodes must be able to `import novafabric`. The recommended approach
is a shared-filesystem venv:

```bash
# On the submit node, with the shared FS mounted:
python3 -m venv /lustre/scratch/myproject/nova-venv
/lustre/scratch/myproject/nova-venv/bin/pip install novafabric
```

Then tell the runner which Python interpreter to use:

```bash
export NOVAFABRIC_SLURM_VENV_PYTHON=/lustre/scratch/myproject/nova-venv/bin/python3
```

If the venv's site-packages are not self-contained (e.g. you have editable
installs or a non-standard layout), you can also specify the site-packages path
to prepend to `PYTHONPATH` on the compute node:

```bash
export NOVAFABRIC_SLURM_VENV_SITE=/lustre/scratch/myproject/nova-venv/lib/python3.12/site-packages
```

Alternatively, install NovaFabric at the same absolute path on every compute
node and ensure that path is on the default `PYTHONPATH`. Shared-FS venv is
simpler and is the validated approach.

#### sitecustomize injection

This is how wire-level capture fires on compute nodes.

Before submitting the job, `SlurmRunner` writes `sitecustomize.py` to the
capsule directory. The capsule directory is on shared filesystem, so it is
visible to the compute node. The `sbatch --wrap` script prepends the capsule
directory to `PYTHONPATH`. When the compute node's Python starts, it
auto-loads `sitecustomize`, which installs NovaFabric's HTTP hooks before
any user code runs.

Without this injection, the workload runs bare on the compute node: no hooks
install, `model-calls.jsonl` is created but never written to, and the capsule
appears empty. This was the bug fixed in v0.6.11.

If `SlurmRunner` cannot write `sitecustomize.py` to the capsule directory
(e.g. permissions error, filesystem not mounted), the runner returns
`runner_status="failed_setup"` with a diagnostic message before submitting
the job. It does not silently submit a job that will produce empty capsules.

#### Environment variables reference

| Variable | Required | Description |
|----------|----------|-------------|
| `NOVAFABRIC_SLURM_SHARED_DIR` | strongly recommended | Base path on shared filesystem for capsule directories and live test isolation |
| `NOVAFABRIC_SLURM_VENV_PYTHON` | for live tests | Path to Python interpreter in the compute-node-accessible venv; used by the `test_sitecustomize_fires_on_compute` integration test |
| `NOVAFABRIC_SLURM_VENV_SITE` | optional | Site-packages path to prepend to compute-node `PYTHONPATH` |
| `NOVAFABRIC_SLURM_PARTITION` | per-site | Default partition for the site env template |
| `NOVAFABRIC_URL_REGISTRY` | optional | Path to a site-local URL registry file (see section 4) |

`NOVAFABRIC_CAPSULE_DIR` and `NOVAFABRIC_SPAN_ID` are set automatically by the
orchestrator and injected into the sbatch wrap script. Do not set them manually
when using `nova capture`.

#### runner_options for SlurmRunner

Pass via `--runner-option key=value` on the CLI or via `.novafabric/runners.yaml`:

| Key | Required | Description |
|-----|----------|-------------|
| `partition` | yes | SLURM partition (queue) to submit to |
| `account` | no | SLURM account / project (`--account`) |
| `qos` | no | Quality of service (`--qos`) |
| `time` | no | Wall-clock limit, e.g. `"01:00:00"` (`--time`). If unset, derived from `--timeout` |
| `nodes` | no | Number of nodes (`--nodes`). Default 1 |
| `gres` | no | Generic resources, e.g. `"gpu:a100:1"` (`--gres`) |
| `mem` | no | Memory request, e.g. `"16G"` (`--mem`) |
| `constraint` | no | Node feature constraint (`--constraint`) |
| `poll_interval_s` | no | How often to poll for job state, in seconds. Default 5.0 |
| `output_dir` | no | Where to write `slurm-<jobid>.out/.err`. Default: capsule directory |

#### State polling: scontrol and sacct

The runner queries job state using `scontrol show job <jobid>` as the primary
source and falls back to `sacct` if `scontrol` returns nothing (which happens
approximately 5 minutes after a job completes, when `slurmctld` forgets it).

This two-path design matters because many development and minimal SLURM clusters
do not run `slurmdbd`. Without `slurmdbd`, `sacct` times out or returns no data.
`scontrol` reads directly from `slurmctld` memory and works without `slurmdbd`.
The `sacct` fallback handles long-running post-completion queries where
`scontrol` is no longer useful.

If your cluster runs `slurmdbd`, both paths work. If it does not, the primary
`scontrol` path handles the common case.

#### Basic invocation

```bash
nova capture \
  --runner slurm \
  --runner-option partition=gpu \
  --runner-option gres=gpu:a100:1 \
  --runner-option time=01:00:00 \
  --runner-option mem=16G \
  python my_agent.py
```

#### Site environment template

A ready-made environment template is at:

```
site-config/examples/slurm-hpc/nova-env.sh
```

Copy it to `site-config/local/slurm-hpc/nova-env.sh` (the `local/` directory
is gitignored), fill in your site values, and source it before running
`nova capture`:

```bash
cp site-config/examples/slurm-hpc/nova-env.sh site-config/local/slurm-hpc/nova-env.sh
# Edit the file — set NOVAFABRIC_SLURM_SHARED_DIR, NOVAFABRIC_SLURM_VENV_PYTHON, partition
source site-config/local/slurm-hpc/nova-env.sh
nova capture --runner slurm --runner-option partition=$NOVAFABRIC_SLURM_PARTITION python my_agent.py
```

Never commit the filled-in `local/` copy — it contains site-specific paths.

#### Project-level SLURM defaults in runners.yaml

```yaml
schema_version: "0.1.0"
default_runner: slurm
runners:
  slurm:
    partition: gpu
    qos: high
    constraint: a100
    gres: "gpu:a100:1"
    mem: "32G"
    time: "02:00:00"
```

#### Running live integration tests against a real cluster

The test suite includes opt-in live tests that require a reachable SLURM cluster:

```bash
# Minimal: echo test (no wire capture)
export NOVAFABRIC_SLURM_SHARED_DIR=/home/vagrant
uv run pytest tests/test_runners_slurm.py::TestSlurmRunnerLiveSmoke::test_echo_succeeds -v

# Full wire-capture e2e (requires venv on shared FS)
export NOVAFABRIC_SLURM_SHARED_DIR=/home/vagrant
export NOVAFABRIC_SLURM_VENV_PYTHON=/home/vagrant/novafabric-venv/bin/python3
uv run pytest tests/test_runners_slurm.py::TestSlurmRunnerLiveSmoke -k sitecustomize -v
```

Both tests were validated against a 3-node cluster running Vagrant + libvirt.
See [v0.6.12 release notes](releases/v0.6.12.md) for the full validation summary.

### 3.4 Kubernetes (KubernetesRunner)

**Works today.** The workload runs as a Kubernetes `Job`. The runner submits the
manifest via `kubectl apply`, polls for Job completion, retrieves logs via
`kubectl logs`, copies capsule artifacts back to the submit node via `kubectl cp`,
and then deletes the Job.

**Requirements:**

- `kubectl` on `PATH` configured with a reachable cluster (`kubectl cluster-info`
  must succeed).
- A target namespace with RBAC permissions for the executing principal:
  `create`, `get`, `watch`, `delete` on `jobs` and `pods`; `get` on
  `pods/log` and `pods/exec` (for `kubectl cp`). No `cluster-admin` required.
- A container image that has NovaFabric installed (same requirement as DockerRunner).

**Basic invocation:**

```bash
nova capture \
  --runner kubernetes \
  --runner-option image=myorg/agent-runtime:abc1234 \
  --runner-option namespace=novafabric-runs \
  python my_agent.py
```

**All `runner_options` for KubernetesRunner:**

| Key | Required | Description |
|-----|----------|-------------|
| `image` | yes | Fully-qualified image reference |
| `namespace` | yes | Target namespace (must exist with RBAC) |
| `service_account` | no | Pod `serviceAccountName` (recommended for least-privilege) |
| `node_selector` | no | Dict of node selector labels |
| `resources` | no | Container resources block (requests and limits) |
| `poll_interval_s` | no | Job status poll interval in seconds. Default 2.0 |

**Example resources block:**

```yaml
runners:
  kubernetes:
    namespace: novafabric-runs
    service_account: novafabric-runner
    image: myorg/agent-runtime:latest
    resources:
      requests:
        cpu: "2"
        memory: "4Gi"
      limits:
        cpu: "4"
        memory: "8Gi"
```

**Security posture:** The KubernetesRunner sets `allowPrivilegeEscalation: false`,
`privileged: false`, `hostNetwork: false`, `hostPID: false`, `hostIPC: false`
on every pod it submits. These are hard-coded in the job manifest builder and
cannot be overridden via `runner_options`.

**Capsule artifact sync:** After the workload exits, the runner runs `kubectl cp`
to copy the in-pod capsule directory (`/novafabric/capsule`) back to the
submit node. If `kubectl cp` fails (e.g. the pod exited before the copy ran),
the runner records a warning in `stderr` but still returns the workload's
exit code. Inspect the capsule directory to determine whether artifacts were
recovered.

**Known limitation (v0.6):** The KubernetesRunner does not surface the workload's
exact exit code when the Job fails. It reports `0` on `succeeded` and `1` on
`failed`. Exact exit codes are queued for a v0.6.x follow-up.

---

## 4. URL registry configuration

The URL registry is a YAML file that maps outbound URL substrings to AI provider
labels. Wire-level hooks consult this registry on every outbound HTTP call to
decide whether to record it and how to classify the `gen_ai.system` field in
`model-calls.jsonl`.

### Bundled defaults

The vendored registry (`src/novafabric/capture/hooks/url_registry.yaml`)
covers the providers below out of the box:

| Match pattern | Provider label |
|---------------|---------------|
| `api.openai.com` | `openai` |
| `api.anthropic.com` | `anthropic` |
| `api.cohere.ai` | `cohere` |
| `api.together.xyz` | `together` |
| `api.mistral.ai` | `mistral` |
| `api.replicate.com` | `replicate` |
| `bedrock-runtime.` | `aws.bedrock` (all regions) |
| `bedrock.` | `aws.bedrock` (control plane) |
| `localhost:11434` | `ollama` |
| `127.0.0.1:11434` | `ollama` |

Ollama at its default port (11434) is captured with no configuration. AWS
Bedrock is captured across all regions (the match string covers any hostname
containing `bedrock-runtime.`).

### Override hierarchy

The registry is loaded using this precedence (first match wins):

1. `$NOVAFABRIC_URL_REGISTRY` — absolute path to a YAML file; explicit per-run
   override.
2. `~/.novafabric/url_registry.yaml` — per-user override; applies to all
   runs by this user.
3. Vendored file inside the package — applies when neither override is present.

**An override file replaces the vendored default entirely.** It does not merge.
If your override omits `api.openai.com`, calls to that endpoint will not be
captured. Always copy in the entries you still need from the vendored file, or
include them explicitly in your override.

### Adding a private inference server

**Case 1: Ollama on a non-default port**

Set `OLLAMA_BASE_URL` (used by `langchain_ollama`) or `OLLAMA_HOST` (used by the
raw `ollama` SDK) to the actual server address. The URL registry reads these
environment variables **at call time**, so they work even when loaded by
`python-dotenv` inside the workload — no manual registry file needed:

```bash
# Tunnel on port 11437:
export OLLAMA_BASE_URL=http://localhost:11437
nova capture python agent.py

# Or in a .env file (loaded by python-dotenv in the workload):
echo 'OLLAMA_BASE_URL=http://localhost:11437' >> .env
nova capture python agent.py
```

If you prefer an explicit registry entry (e.g. for a shared site config), copy
`site-config/examples/local-ollama/url_registry.yaml` to
`~/.novafabric/url_registry.yaml` and add an entry for the port:

```yaml
schema_version: "0.1.0"
patterns:
  - match: "localhost:11437"
    gen_ai_system: "ollama"
    transport: "http"
  - match: "localhost:11434"
    gen_ai_system: "ollama"
    transport: "http"
  - match: "127.0.0.1:11434"
    gen_ai_system: "ollama"
    transport: "http"
  - match: "api.openai.com"
    gen_ai_system: "openai"
    transport: "http"
```

**Case 2: vLLM, TGI, llama.cpp, or any OpenAI-compatible server on a remote host**

Use `site-config/examples/remote-inference/url_registry.yaml` as a starting
point. Set `match` to `host:port` of the inference server:

```yaml
schema_version: "0.1.0"
patterns:
  - match: "gpu-node-01.cluster.local:8000"
    gen_ai_system: "vllm"
    transport: "http"
    notes: "vLLM inference server on gpu-node-01"
  - match: "api.openai.com"
    gen_ai_system: "openai"
    transport: "http"
```

**Case 3: SSH port-forward or kubectl port-forward to a remote server**

If the server is reachable locally via a port forward, match the local address:

```yaml
  - match: "localhost:8001"
    gen_ai_system: "vllm"
    transport: "http"
    notes: "port-forwarded from gpu-node-01:8000"
```

**Case 4: Site-wide override for a SLURM cluster**

Put the registry file on shared filesystem and reference it in the site env file:

```bash
# In site-config/local/slurm-hpc/nova-env.sh:
export NOVAFABRIC_URL_REGISTRY=/lustre/scratch/myproject/nova-url-registry.yaml
```

Source the env file before submitting jobs. The `SlurmRunner` forwards
`NOVAFABRIC_URL_REGISTRY` to the compute node's environment via the wrap script,
so the compute node's hooks load the site registry.

### Registry file format

The `match` field is a substring check against the full URL string (scheme,
host, port, and path). The first matching entry wins. Order entries from most
specific to least specific.

```yaml
schema_version: "0.1.0"
patterns:
  - match: "host:port/v1"          # more specific path prefix first
    gen_ai_system: "myprovider"
    transport: "http"
    notes: "Optional human-readable note; not emitted in model-calls.jsonl"
  - match: "host:port"             # broader fallback
    gen_ai_system: "myprovider"
    transport: "http"
```

Calls to URLs that match no registry entry are not recorded. This is
intentional — it avoids capturing unrelated HTTP traffic (health checks,
metrics endpoints, object storage) that happens to come from the same process.

---

## 5. Troubleshooting

### model-calls.jsonl is empty or missing

**Cause:** `sitecustomize.py` was not on `PYTHONPATH` when the workload's Python
started, so the hooks never installed.

**For LocalRunner:** This should not happen — the runner creates a temporary
directory with `sitecustomize.py` and injects it via `PYTHONPATH` before
launching the subprocess. If the file is missing, check whether the workload
spawns its own subprocess that does not inherit `PYTHONPATH`.

**For SlurmRunner:** The capsule directory must be on shared filesystem. If the
compute node cannot see `capsule_dir`, it cannot load `sitecustomize.py`. Check:

```bash
# On the compute node (or via srun):
ls -la /path/to/capsule_dir/sitecustomize.py
```

If the file is missing, the capsule directory is not on shared filesystem. Move
it to a path that all nodes can reach.

**For KubernetesRunner:** The image must have NovaFabric installed. The runner
injects `PYTHONPATH` pointing at `/novafabric/capsule` inside the pod. If the
image's Python cannot `import novafabric`, the hook loader will print an error to
stderr and skip capture. Check the pod logs:

```bash
kubectl logs <pod-name> -n <namespace>
```

Look for `[novafabric] hook install failed:` in stderr output.

### Capsule directory not visible from compute nodes

**Symptom:** Job exits with a non-zero code immediately, or
`runner_status="failed_setup"` with a message about the capsule directory.

**Cause:** The capsule directory is on node-local storage.

**Fix:** Set `NOVAFABRIC_SLURM_SHARED_DIR` to a path on shared filesystem
before running `nova capture`. The orchestrator will create capsule directories
under that base path.

### sbatch submission fails

Check the exit code and stderr returned in `RunnerJobResult`. Common causes:

- `partition` does not exist on this cluster. Run `sinfo -s` to list available
  partitions.
- The user does not have permission to submit to the partition. Check with
  your cluster administrator.
- `sbatch` binary is not on `PATH` on the submit node.

```bash
which sbatch && sbatch --version
```

### scontrol shows no job after completion

`scontrol show job <jobid>` returns "Invalid job id" once `slurmctld` has
forgotten the job (typically 5 minutes after completion). This is normal. The
SlurmRunner uses `sacct` as a fallback in this window. If `sacct` also fails,
confirm that `slurmdbd` is running on the cluster:

```bash
systemctl status slurmdbd
sacct -j <jobid> --format=JobID,State,ExitCode --noheader
```

If neither tool returns data, the runner will time out polling and return
`runner_status="timeout"`. Capsule artifacts may still be present on shared
filesystem; check the capsule directory directly.

### Docker image pull fails

`docker run` exit code 125 indicates a Docker daemon-level failure, which
includes image pull errors. Check:

```bash
docker pull myorg/agent-runtime:abc1234
```

Make sure the image reference is correct and the daemon has pull access
(credentials, registry reachability). The DockerRunner only forwards
`NOVAFABRIC_*` env vars into the container, not `DOCKER_AUTH_CONFIG` or
registry credentials. Pre-pull the image before invoking `nova capture` if
you cannot configure pull credentials at the daemon level.

### Kubernetes kubectl cp fails

The `kubectl cp` step runs after the workload exits. If the pod's container
has already terminated and been garbage-collected before `kubectl cp` starts,
the copy will fail. This is more likely when:

- `--timeout` is very short relative to copy time.
- The cluster's pod garbage collection (`completedJobsHistoryLimit`) runs
  aggressively.

Check the runner metadata on the capsule:

```bash
nova show <run-id>
# or read capsule.yaml directly:
cat ~/.novafabric/capsules/<run-id>/capsule.yaml
```

Look for `host.runner.metadata.pod_name`. If present, the pod was identified;
if the copy failed, you may be able to recover artifacts by re-running `kubectl
cp` manually while the pod still exists.

### Wire-capture records classified as gen_ai.system=unknown

The URL being called does not match any entry in the active URL registry.
The call is captured (hooks are working) but cannot be classified.

Add an entry to your URL registry override (see section 4). Then check the
active registry path:

```bash
# Which registry file is being loaded?
python3 -c "
from novafabric.capture.hooks._url_registry import load_url_registry, _VENDORED_DEFAULT_PATH
import os
env = os.environ.get('NOVAFABRIC_URL_REGISTRY', '')
user = os.path.expanduser('~/.novafabric/url_registry.yaml')
print('NOVAFABRIC_URL_REGISTRY:', env or '(not set)')
print('user override exists:', os.path.exists(user))
print('vendored default:', _VENDORED_DEFAULT_PATH)
"
```

---

## 5b. NovaSeal configuration (cryptographic signing, v0.10)

NovaSeal signs capsules with ECDSA P-256 and timestamps them via RFC 3161.
It is **opt-in** — if `~/.novafabric/novaseal.yaml` (or `NOVAFABRIC_SEAL_CONFIG`)
is absent, capture works exactly as before.

For the full configuration reference (all profiles, env vars, Docker/SLURM
patterns, path resolution order, troubleshooting) see:
**[docs/novaseal-configuration.md](novaseal-configuration.md)**

**Quick start — local key + minimal config:**

```bash
mkdir -p ~/.novafabric
openssl ecparam -name prime256v1 -genkey -noout | \
  openssl pkcs8 -topk8 -nocrypt -out ~/.novafabric/seal.key
openssl req -new -x509 -key ~/.novafabric/seal.key -days 3650 \
  -out ~/.novafabric/seal.crt -subj "/CN=NovaSeal-$(hostname)"
```

```yaml
# ~/.novafabric/novaseal.yaml
profile: local
key_path: ~/.novafabric/seal.key
cert_path: ~/.novafabric/seal.crt
tsa_url: https://freetsa.org/tsr
merkle_db: ~/.novafabric/novaseal-merkle.db
```

**SLURM / shared-FS deployments:** set `merkle_db:` to a path on the shared
filesystem. See [novaseal-configuration.md §3.3](novaseal-configuration.md#33-slurm-and-shared-filesystem-deployments).

**Docker deployments:** set `NOVAFABRIC_SEAL_CONFIG` to the mounted config path
and configure `merkle_db:` inside it. See [novaseal-configuration.md §3.2](novaseal-configuration.md#32-docker-and-container-deployments).

**Verify a sealed capsule:**

```bash
nova verify /path/to/capsule/
# signature_ok=True, timestamp_ok=True, log_integrity_ok=True
```

**Network note:** The TSA request (`tsa_url`) requires outbound HTTPS on port 443.
On air-gapped clusters, set `tsa_url: ""` to skip timestamping, or run a private
TSA inside the cluster and point `tsa_url` at it.

---

## 6. What is not supported yet

The following capabilities are **not implemented** in the current release.
They are on the roadmap but have no code in `main`.

| Capability | Status | Target |
|------------|--------|--------|
| NovaSeal: Sigstore keyless signing | planned (ADR-0041 ADR-002) | v0.2 |
| NovaSeal: Cloud KMS (AWS KMS, Azure KV, GCP KMS) | planned | v0.2 |
| NovaSeal: X.509 HSM / PKCS#11 | planned | v0.2 |
| NovaSeal: Postgres Merkle log | planned (ADR-0040) | v0.2 |
| Federation across clusters | future design (ADR-0021 §federation-topology) | v0.9+ |
| Parent/child capsule relationships | future design (ADR-0039) | v1.x |
| interLink K8s-to-SLURM integration | proposed (ADR-0028, no code) | unscheduled |
| Third-party runner plugins (AWS Batch, Modal, etc.) | planned (ADR-0025 §B) | v0.6.x+ |
| Registry merge semantics (override + vendored combined) | planned | v0.6.x |

Do not configure production workflows that depend on any of the above. If you
need one of these capabilities, open an issue or follow the RFC process
described in [design/governance/RFC-0000-rfc-process.md](../design/governance/RFC-0000-rfc-process.md).

---

## 7. Docker Compose deployment (`nova serve` + dashboard)

The bundled `deploy/docker/docker-compose.yml` runs the full dev stack
(Postgres + nova-serve) in a single command. This section documents every
data-path decision you must get right for a correct deployment; mistakes here
result in silent empty tabs in the dashboard rather than error messages.

### 7.1 Stack profiles

| Profile | Services started | Command |
|---------|-----------------|---------|
| *(none — default)* | `postgres` + `nova` | `make dev-up` |
| `prod` | + ClickHouse + NATS + Kafka + PgBouncer + JanusGraph | `make prod-up` |

Both profiles are defined in `deploy/docker/docker-compose.yml`. The `nova`
service depends on `postgres` being healthy before it starts.

> **`make update` note:** rebuilds the `nova` image and does a rolling restart
> with `--no-deps`, so it intentionally does not restart Postgres. If Postgres
> is not already running (e.g. first deploy after a reboot), run
> `make dev-up` instead, or the `nova` container will wait forever for the DB.

### 7.2 Volume mounts

The `nova` service uses three mounts. All host paths default to
`~/novafabric-data/` and can be overridden with `NOVA_DATA_DIR`.

| Container path | Default host path | Purpose |
|---------------|-------------------|---------|
| `/data/capsules` | `$NOVA_DATA_DIR/capsules` | Run capsule directories (written by `nova capture`, read by all tabs) |
| `/data/nova` | `$NOVA_DATA_DIR/nova` | `NOVAFABRIC_HOME` inside the container — registry DB, serve token, audit log |
| `/data/kuzu` | Docker named volume `kuzu-data` | KuzuDB knowledge graph store |

**All testbench workloads must write capsules to the host path that maps to
`/data/capsules`.** Do not use a relative `capsules/` path or a symlink inside
the testbench; set `NOVAFABRIC_HOME` (or `NOVA_CAPSULE_DIR`) explicitly.
See `deploy/docker/docker-compose.yml` for the exact mount syntax.

### 7.3 Data-path environment variables

The following variables control where the `nova` process inside the container
reads and writes data. Wrong values cause dashboard tabs to show empty results
without any error.

| Variable | Value in Docker | Default outside Docker | Effect |
|----------|----------------|----------------------|--------|
| `NOVAFABRIC_HOME` | `/data/nova` | `~/.novafabric` | Registry DB, serve token, audit log root |
| `NOVAFABRIC_EVIDENCE_DIR` | `/data/capsules/evidence` | `~/.novafabric/evidence` | Where `nova export-evidence` writes ZIPs; where the Evidence tab reads them |
| `NOVAFABRIC_METADATA_BACKEND` | `postgres` | `sqlite` | MetadataStore backend |
| `NOVAFABRIC_METADATA_DSN` | `postgresql://nova:nova@postgres:5432/nova` | *(SQLite file)* | DB connection string |
| `NOVAFABRIC_SERVER_BACKEND` | `postgres` | *(sqlite)* | Server-mode Postgres DSN alias |
| `NOVAFABRIC_POSTGRES_DSN` | same as above | — | Used by DB migration commands |
| `NOVA_WATCHER_BACKEND` | `auto` | `auto` | `CapsuleWatcher` backend: `auto` (polling, or watchdog if installed), `polling`, `watchdog` |
| `NOVA_WATCHER_INTERVAL` | `2.0` | `2.0` | Poll interval in seconds for `PollingBackend` and `nova ingest-capsule --watch` |

> **Why `NOVAFABRIC_EVIDENCE_DIR` matters:** The serve app defaults to
> `$HOME/.novafabric/evidence` which inside the container resolves to
> `/root/.novafabric/evidence` — an ephemeral path that is not mounted and
> disappears on container restart. The correct path is `/data/capsules/evidence`
> (inside the capsule volume), which persists across restarts and is written to
> by the testbench export stage.

### 7.4 Fresh deployment checklist

When deploying to a new host for the first time:

```bash
# 1. Clone and pull
git clone https://github.com/novafabric/novafabric.git
cd novafabric
git pull

# 2. Create the data directories on the host
mkdir -p ~/novafabric-data/capsules ~/novafabric-data/nova

# 3. Start the full stack (builds image, starts postgres + nova-serve)
make dev-up
# prints: Dashboard: http://localhost:4321/dashboard?token=<token>

# 4. (Optional) SSH tunnel if running on a remote server
#    Run on your laptop:
ssh -N -L 4321:localhost:4321 <user>@<server> &
open http://localhost:4321/dashboard?token=<token>
```

If the dashboard opens but tabs are empty, check:

1. **Runs tab empty** — no capsules in `~/novafabric-data/capsules/` on the host,
   or the `runs_cache` index is stale. Force a full re-index:
   ```bash
   docker compose exec nova uv run nova ingest-capsule --all
   # or from the host:
   nova ingest-capsule --all --capsule-dir ~/novafabric-data/capsules
   ```
   `nova serve` also re-indexes on startup and every 2 s via `CapsuleWatcher`
   (`NOVA_WATCHER_BACKEND=auto`, `NOVA_WATCHER_INTERVAL=2.0`).
2. **Evidence tab empty** — `NOVAFABRIC_EVIDENCE_DIR` not set or pointing at the
   wrong path. Verify with:
   `docker compose exec nova sh -c 'echo $NOVAFABRIC_EVIDENCE_DIR'`
3. **Container stuck at "waiting for postgres"** — Postgres not running.
   Run `make dev-up` (not `make update`) to bring up the full stack.

### 7.5 Remote access via SSH tunnel (`make nova-dashboard`)

The `lpt/` orchestrator project includes a `nova-dashboard` Makefile target
that automates the SSH tunnel + token fetch in one command:

```bash
# From ~/scratch/lpt/ on your laptop:
make nova-dashboard
# → ensures postgres + nova are up on n1
# → fetches the live token
# → opens an SSH tunnel on localhost:4321
# → prints the dashboard URL
# → keeps tunnel open until Ctrl-C
```

The target uses `docker compose exec nova` (service name, not container name)
to read the token — this is robust to container renames across deploys.
