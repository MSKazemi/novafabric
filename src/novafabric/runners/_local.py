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
from typing import Any

from novafabric.runners._sitecustomize import HOOK_LOADER as _HOOK_LOADER
from novafabric.runners._types import (
    RunnerJobResult,
    RunnerJobSpec,
    attach_interrupted_result,
    interrupted_job_result,
)

#: Wall-clock deadline when the caller passes none (``nova capture`` without
#: ``--timeout``; the GitHub Action's empty ``timeout`` input).
DEFAULT_TIMEOUT_S = 600.0

#: Seconds an interrupted workload gets to exit after the forwarded signal, before SIGKILL.
INTERRUPT_GRACE_S = 10.0
_DRAIN_TIMEOUT_S = 5.0
#: Bytes of each of stdout and stderr kept in memory (and in the capsule); the rest
#: is read and counted, never buffered (review capture-runners-10).
OUTPUT_CAP_BYTES = 64 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class _Drain:
    """Read one workload pipe on a daemon thread into a bounded buffer.

    The runner waits for the workload to *exit*, not for its pipes to close: a
    detached child (``setsid``, a double-forked server) inherits them and may hold
    them open for its whole life. Reading continues in the background so the
    workload never blocks on a full pipe; what was read by the time the runner
    stops waiting is the captured output.
    """

    def __init__(self, pipe: Any, name: str) -> None:
        self.name = name
        # A private duplicate: once the Popen object closes its pipe the number may
        # be reused, and this thread must never read some other file. The thread
        # closes it at EOF.
        self._fd = os.dup(pipe.fileno())
        self._buf = bytearray()
        self.dropped = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=f"nf-drain-{name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = os.read(self._fd, _READ_CHUNK)
                if not chunk:
                    return
                with self._lock:
                    keep = max(0, OUTPUT_CAP_BYTES - len(self._buf))
                    self._buf += chunk[:keep]
                    self.dropped += len(chunk) - min(keep, len(chunk))
        except OSError:
            return
        finally:
            os.close(self._fd)

    def collect(self, grace: float) -> tuple[bytes, bool]:
        """``(output, complete)``: complete is False when a process still holds the pipe."""
        self._thread.join(grace)
        with self._lock:
            data = bytes(self._buf)
            if self.dropped:
                data += (
                    f"\n[novafabric] {self.dropped} byte(s) of {self.name} not kept "
                    f"(capture keeps the first {OUTPUT_CAP_BYTES})\n"
                ).encode()
        return data, not self._thread.is_alive()


def _collect(drains: tuple[_Drain, _Drain], grace: float) -> tuple[bytes, bytes, dict[str, Any]]:
    """Both streams, a note when output may be incomplete, and truncation counts."""
    stdout, out_done = drains[0].collect(grace)
    stderr, err_done = drains[1].collect(grace if out_done else 0.0)
    if not (out_done and err_done):
        stderr += (
            b"\n[novafabric] a process that outlived the workload still holds its output "
            b"open; output written after the workload exited is not captured\n"
        )
    metadata: dict[str, Any] = {
        f"{d.name}_truncated_bytes": d.dropped for d in drains if d.dropped
    }
    return stdout, stderr, metadata

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


def _interrupted(drains: tuple[_Drain, _Drain], exc: BaseException) -> RunnerJobResult:
    """What an interrupted, already stopped workload left behind."""
    try:
        # The group is dead, so the pipes are at EOF; the bound covers a
        # descendant that escaped the group and still holds one open.
        stdout, stderr, metadata = _collect(drains, _DRAIN_TIMEOUT_S)
    except (Exception, KeyboardInterrupt):
        # A repeated Ctrl-C here is the same request as the first: stop.
        stdout, stderr, metadata = b"", b"", {}
    return interrupted_job_result(exc, stdout=stdout, stderr=stderr, runner_metadata=metadata)


class _ForwardTarget:
    """The process group :func:`_forward_termination` forwards to, once it exists."""

    def __init__(self) -> None:
        self.pgid: int | None = None
        self.pending: int | None = None

    def attach(self, pgid: int) -> None:
        """Start forwarding to *pgid*; deliver a signal that arrived before it existed."""
        self.pgid = pgid
        if self.pending is not None:
            _signal_group(pgid, self.pending)
            raise SystemExit(128 + self.pending)


@contextmanager
def _forward_termination() -> Iterator[_ForwardTarget]:
    """While the workload runs, SIGTERM/SIGHUP to ``nova capture`` reach the workload too.

    The workload runs in its own session (so a timeout can kill its whole tree), so
    signals sent to ``nova capture`` — a scheduler's SIGTERM, ``kill``, a closing
    terminal — never reached it: ``nova capture`` died and the workload ran on,
    unsupervised, writing into a capsule nothing would finalise (review
    capture-runners-03). The handler forwards the signal and exits ``128 + signum``;
    :meth:`LocalRunner.run` then waits out the grace period. Signal handlers can be
    set only on the main thread; elsewhere this is a no-op.

    The handler is installed *before* the workload is spawned: installed after
    ``Popen`` returned, a signal landing in between killed ``nova capture`` outright
    and orphaned the workload. A signal that arrives before the group exists is
    held and delivered by :meth:`_ForwardTarget.attach`.
    """
    target = _ForwardTarget()
    if threading.current_thread() is not threading.main_thread():
        yield target
        return

    def forward(signum: int, _frame: FrameType | None) -> None:
        if target.pgid is None:
            target.pending = signum  # no workload yet: attach() delivers it
            return
        _signal_group(target.pgid, signum)
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, forward) for sig in _FORWARDED_SIGNALS}
    try:
        yield target
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

    @staticmethod
    def _supervise(
        proc: subprocess.Popen[bytes], pgid: int, target: _ForwardTarget, timeout: float
    ) -> RunnerJobResult:
        """Wait for the workload; stop its whole group on a timeout or an interruption.

        The deadline applies to the workload's *exit* (``proc.wait``); its pipes are
        drained in the background (:class:`_Drain`), so a detached child that keeps
        them open can neither stretch the run nor turn a success into a timeout
        (review capture-runners-10).
        """
        drains = (_Drain(proc.stdout, "stdout"), _Drain(proc.stderr, "stderr"))
        try:
            target.attach(pgid)  # delivers a signal that arrived during the spawn
            proc.wait(timeout=timeout)
            stdout, stderr, metadata = _collect(drains, _DRAIN_TIMEOUT_S)
            return RunnerJobResult(
                exit_code=proc.returncode,
                runner_status="completed",
                stdout=stdout,
                stderr=stderr,
                runner_metadata=metadata,
            )
        except subprocess.TimeoutExpired:
            # Kill the entire process group (agent + all descendants).
            _signal_group(pgid, signal.SIGKILL)
            proc.wait()
            stdout, stderr, metadata = _collect(drains, _DRAIN_TIMEOUT_S)
            return RunnerJobResult(
                exit_code=124,
                runner_status="timeout",
                stdout=stdout,
                stderr=stderr + b"\n[novafabric] capture timeout",
                runner_error=f"workload exceeded {timeout}s wall-clock deadline",
                runner_metadata=metadata,
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
                attach_interrupted_result(exc, _interrupted(drains, exc))
            raise

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
                with _forward_termination() as target:
                    proc = subprocess.Popen(
                        spec.command,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    )
                    pgid = proc.pid  # start_new_session: the child leads its own group
                    return self._supervise(proc, pgid, target, timeout)
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
