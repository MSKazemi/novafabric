"""Type definitions for the runner abstraction (ADR-0025).

Concrete runners (`LocalRunner`, future `DockerRunner`, etc.) implement
the :class:`RunnerSpec` protocol. The orchestrator depends only on this
module — never on a concrete runner.
"""
from __future__ import annotations

import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class RunnerJobSpec:
    """What the orchestrator hands to a runner.

    Attributes:
        run_id: ULID, allocated by the orchestrator before submission.
        command: argv to execute inside the runner (e.g. ``[sys.executable, "agent.py"]``).
        capsule_dir: local path the orchestrator owns; runners must
            ensure capsule artifacts are present here on local FS by
            the time :meth:`RunnerSpec.run` returns.
        env: env vars to inject into the workload (already includes
            ``NOVAFABRIC_CAPSULE_DIR``, ``NOVAFABRIC_SPAN_ID``, and
            ``PYTHONPATH`` containing the sitecustomize loader).
        timeout_s: wall-clock deadline; ``None`` = use the runner's
            default. Passed through opaquely.
        runner_options: runner-specific extras (image name, k8s
            namespace, slurm partition, etc.). Schema is per-runner;
            the orchestrator does not interpret.
    """

    run_id: str
    command: list[str]
    capsule_dir: Path
    env: dict[str, str]
    timeout_s: float | None = None
    runner_options: dict[str, Any] | None = None


@dataclass
class RunnerJobResult:
    """What the orchestrator gets back from a runner.

    Attributes:
        exit_code: workload's exit code (0 = success). When the runner
            failed to start the workload, ``exit_code`` is ``127`` and
            ``runner_status`` is ``"failed_setup"``.
        runner_status: ``"completed"`` | ``"failed_setup"`` | ``"timeout"`` | ``"killed"``
            | ``"interrupted"`` (carried on an interruption, see
            :func:`attach_interrupted_result`).
        stdout: captured stdout bytes.
        stderr: captured stderr bytes.
        runner_error: human-readable detail when ``runner_status``
            is not ``"completed"``.
        runner_metadata: runner-specific telemetry — k8s pod name,
            slurm job id, docker container id, etc. Persisted in
            ``capsule.yaml`` under ``host.runner.metadata``.
    """

    exit_code: int
    runner_status: str
    stdout: bytes = b""
    stderr: bytes = b""
    runner_error: str | None = None
    runner_metadata: dict[str, Any] = field(default_factory=dict)


_INTERRUPTED_RESULT_ATTR = "_novafabric_runner_result"


def attach_interrupted_result(exc: BaseException, result: RunnerJobResult) -> None:
    """Attach *result* to the interruption a runner is about to re-raise.

    A runner that was interrupted (Ctrl-C, or a forwarded SIGTERM raising
    ``SystemExit``) and has **already stopped its workload** attaches what it
    collected, then re-raises the *same* exception. Every caller still sees exactly
    the interruption it would have seen — exit code 130 / 128+signal, the same
    ``KeyboardInterrupt`` for a Python API caller — while the orchestrator can
    finalise the capsule first (review capture-runners-03). A runner that could not
    stop its workload attaches nothing: finalising a capsule a live workload may
    still write into would seal a snapshot that is already stale.
    """
    setattr(exc, _INTERRUPTED_RESULT_ATTR, result)


def interrupted_result(exc: BaseException) -> RunnerJobResult | None:
    """The result :func:`attach_interrupted_result` put on *exc*, if any."""
    result = getattr(exc, _INTERRUPTED_RESULT_ATTR, None)
    return result if isinstance(result, RunnerJobResult) else None


def interrupt_exit(exc: BaseException) -> tuple[int, str]:
    """``(exit code, cause)`` for an interruption: ``128 + signal`` for the
    ``SystemExit`` a forwarded signal raises, ``130`` (SIGINT) for Ctrl-C."""
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
        try:
            return exc.code, signal.Signals(exc.code - 128).name
        except ValueError:
            return exc.code, f"exit {exc.code}"
    return 130, "SIGINT"


def interrupted_job_result(
    exc: BaseException,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    runner_metadata: dict[str, Any] | None = None,
) -> RunnerJobResult:
    """The ``interrupted`` result for a workload the runner has already stopped."""
    exit_code, cause = interrupt_exit(exc)
    return RunnerJobResult(
        exit_code=exit_code,
        runner_status="interrupted",
        stdout=stdout,
        stderr=stderr,
        runner_error=f"nova capture was interrupted ({cause}); the workload was stopped",
        runner_metadata=runner_metadata or {},
    )


class ContainerEvalError(Exception):
    """Raised by :meth:`DockerRunner.run_eval_container` on infrastructure failure.

    Distinct from the capsule *failing the benchmark* — that is expressed
    via ``EvalResult.passed = False``.  This exception signals that the
    container could not be launched, the digest was malformed, or the
    Docker daemon was unavailable.

    Suite adapter implementations should catch this and re-raise as
    :class:`~novafabric.evals.exceptions.EvalSuiteError` with context.
    """


class RunnerSpec(Protocol):
    """The contract every runner satisfies.

    Synchronous-blocking by design in v0.6 — :meth:`run` does not return
    until the workload completes, fails, times out, or is killed. The
    orchestrator pipes stdout/stderr from the result; live streaming is
    a v0.7 concern (see ADR-0025 §Alternatives).
    """

    name: str
    """Short identifier (``"local"``, ``"docker"``, etc.)."""

    description: str
    """One-line description shown in ``nova capture --help``."""

    def supports(self) -> tuple[bool, str]:
        """Return ``(True, "")`` if usable in the current environment,
        else ``(False, "why-not message")``.

        Examples:
            ``DockerRunner.supports()`` → ``(False, "docker daemon not reachable")``
            ``SlurmRunner.supports()``  → ``(True, "")``
        """
        ...

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        """Spawn the workload, wait for completion, return the result.

        On return, all capsule artifacts must be present at
        ``spec.capsule_dir`` on the local filesystem. Runners that
        execute remotely (Docker volume, k8s sidecar, SLURM shared FS)
        MUST sync back before returning.
        """
        ...
