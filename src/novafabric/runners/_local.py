"""Local-process runner (Runners.1).

The default runner — preserves all behavior that ``CaptureOrchestrator``
had inline before ADR-0025. This is *not* a behavior change: the previous
``_run_subprocess`` method is moved here, and the orchestrator now
delegates to a ``LocalRunner`` instance.

Concrete runners for Docker / Kubernetes / SLURM live in sibling files
(Runners.2 / .3 / .4) and satisfy the same :class:`RunnerSpec` protocol.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from pathlib import Path

from novafabric.runners._sitecustomize import HOOK_LOADER as _HOOK_LOADER
from novafabric.runners._types import RunnerJobResult, RunnerJobSpec


class LocalRunner:
    """Runs the captured workload as a local subprocess.

    Default and only runner shipped in v0.5.x. Future runners
    (Docker/K8s/SLURM) implement the same :class:`RunnerSpec` protocol;
    the orchestrator does not need to know which runner it has.
    """

    name: str = "local"
    description: str = "Run the captured workload as a local subprocess (default)."

    def supports(self) -> tuple[bool, str]:
        # Local execution is always available — that's the contract.
        return True, ""

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        # The orchestrator passes a fully-prepared env (with
        # NOVAFABRIC_CAPSULE_DIR + NOVAFABRIC_SPAN_ID already set), but
        # the sitecustomize loader needs to live in a directory that
        # ends up on PYTHONPATH. We materialize it in a tmpdir for the
        # lifetime of the subprocess.
        with tempfile.TemporaryDirectory(prefix="nf_site_") as site_dir:
            (Path(site_dir) / "sitecustomize.py").write_text(_HOOK_LOADER)

            env = dict(spec.env)
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{site_dir}:{existing}" if existing else site_dir

            timeout = spec.timeout_s if spec.timeout_s is not None else 600.0

            try:
                # start_new_session=True creates a new process group so that
                # on timeout we can kill the entire tree (agent + any SSH/tool
                # children it spawned), not just the immediate child.
                proc = subprocess.Popen(
                    spec.command,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                    return RunnerJobResult(
                        exit_code=proc.returncode,
                        runner_status="completed",
                        stdout=stdout,
                        stderr=stderr,
                    )
                except subprocess.TimeoutExpired:
                    # Kill the entire process group (agent + all descendants).
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = proc.communicate()
                    return RunnerJobResult(
                        exit_code=124,
                        runner_status="timeout",
                        stdout=stdout or b"",
                        stderr=(stderr or b"") + b"\n[novafabric] capture timeout",
                        runner_error=f"workload exceeded {timeout}s wall-clock deadline",
                    )
            except Exception as exc:
                return RunnerJobResult(
                    exit_code=127,
                    runner_status="failed_setup",
                    stderr=str(exc).encode(),
                    runner_error=str(exc),
                )
