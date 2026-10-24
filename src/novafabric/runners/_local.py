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
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from novafabric.runners._sitecustomize import HOOK_LOADER as _HOOK_LOADER
from novafabric.runners._types import RunnerJobResult, RunnerJobSpec, attach_interrupted_result

#: Wall-clock deadline when the caller passes none (``nova capture`` without
#: ``--timeout``; the GitHub Action's empty ``timeout`` input).
DEFAULT_TIMEOUT_S = 600.0

#: Seconds an interrupted workload gets to exit after the forwarded signal, before SIGKILL.
INTERRUPT_GRACE_S = 10.0
_DRAIN_TIMEOUT_S = 5.0

#: Signals that end ``nova capture`` and are forwarded to the workload's process group.
_FORWARDED_SIGNALS = tuple(
    sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if sig
)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_group(proc: subprocess.Popen[bytes], pgid: int, first: int) -> None:
    """Send *first* to the workload's group, wait :data:`INTERRUPT_GRACE_S`, then SIGKILL."""
    _signal_group(pgid, first)
    try:
        proc.wait(timeout=INTERRUPT_GRACE_S)
    except subprocess.TimeoutExpired:
        _signal_group(pgid, signal.SIGKILL)
        proc.wait()
    # Descendants that outlived the group leader still share its group.
    _signal_group(pgid, signal.SIGKILL)


def _interrupted(proc: subprocess.Popen[bytes], exc: BaseException) -> RunnerJobResult:
    """What an interrupted, already stopped workload left behind."""
    try:
        # The group is dead, so the pipes are at EOF; the bound covers a
        # descendant that escaped the group and still holds one open.
        stdout, stderr = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
    except (Exception, KeyboardInterrupt):
        # A repeated Ctrl-C here is the same request as the first: stop.
        stdout, stderr = b"", b""
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
        exit_code = exc.code
        try:
            cause = signal.Signals(exit_code - 128).name
        except ValueError:
            cause = f"exit {exit_code}"
    else:
        exit_code, cause = 130, "SIGINT"
    return RunnerJobResult(
        exit_code=exit_code,
        runner_status="interrupted",
        stdout=stdout or b"",
        stderr=stderr or b"",
        runner_error=f"nova capture was interrupted ({cause}); the workload was stopped",
    )


@contextmanager
def _forward_termination(pgid: int) -> Iterator[None]:
    """While the workload runs, SIGTERM/SIGHUP to ``nova capture`` reach the workload too.

    The workload runs in its own session (so a timeout can kill its whole tree), so
    signals sent to ``nova capture`` — a scheduler's SIGTERM, ``kill``, a closing
    terminal — never reached it: ``nova capture`` died and the workload ran on,
    unsupervised, writing into a capsule nothing would finalise (review
    capture-runners-03). The handler forwards the signal and exits ``128 + signum``;
    :meth:`LocalRunner.run` then waits out the grace period. Signal handlers can be
    set only on the main thread; elsewhere this is a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def forward(signum: int, _frame: FrameType | None) -> None:
        _signal_group(pgid, signum)
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, forward) for sig in _FORWARDED_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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

            timeout = spec.timeout_s if spec.timeout_s is not None else DEFAULT_TIMEOUT_S

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
                pgid = proc.pid  # start_new_session: the child leads its own group
                try:
                    with _forward_termination(pgid):
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
                except BaseException as exc:
                    # Ctrl-C (SIGINT reaches only nova capture's own group) or a
                    # forwarded SIGTERM/SIGHUP: stop the workload, then let the
                    # interruption end the capture. Never leave it running alone.
                    first = signal.SIGINT if isinstance(exc, KeyboardInterrupt) else signal.SIGTERM
                    _stop_group(proc, pgid, first)
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        # The workload is stopped, so the orchestrator may finalise
                        # the capsule before the interruption ends the capture.
                        attach_interrupted_result(exc, _interrupted(proc, exc))
                    raise
            except FileNotFoundError as exc:
                # The commonest first-run mistake is a mistyped command name.
                # Say so in plain words when argv[0] really does not resolve on
                # the workload's PATH. If it DOES resolve, the ENOENT came from
                # somewhere else (e.g. a script whose shebang interpreter is
                # missing), so keep the OS's own wording rather than guess.
                argv0 = spec.command[0] if spec.command else ""
                if argv0 and shutil.which(argv0, path=env.get("PATH")) is None:
                    reason = f"command not found: {argv0}"
                else:
                    reason = str(exc)
                return RunnerJobResult(
                    exit_code=127,
                    runner_status="failed_setup",
                    stderr=reason.encode(),
                    runner_error=reason,
                )
            except Exception as exc:
                return RunnerJobResult(
                    exit_code=127,
                    runner_status="failed_setup",
                    stderr=str(exc).encode(),
                    runner_error=str(exc),
                )
