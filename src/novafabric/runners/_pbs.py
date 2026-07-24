"""PBS Torque / OpenPBS runner (ADR-0025 Runners.5).

Runs the captured workload as a PBS batch job via ``qsub``, polls
``qstat -f`` for completion, and reads stdout/stderr from the job's
output files. The capsule directory MUST be on a filesystem shared
between the submit node and the compute node — typically the cluster's
home or scratch filesystem (NFS, Lustre, GPFS, etc.). The runner does
NOT rsync artifacts; it relies entirely on the shared FS.

This implementation follows the same pattern as :class:`SlurmRunner`:
1. Write a PBS job script to a temporary file inside the capsule dir.
2. Submit via ``qsub <script.pbs>`` and parse the returned job ID.
3. Poll ``qstat -f <job_id>`` until the job enters a terminal state.
4. Read per-job stdout/stderr files written by PBS.

Wire-level capture is enabled by the same sitecustomize injection used
by SlurmRunner: ``sitecustomize.py`` is materialized in the capsule dir
(shared FS) and picked up via PYTHONPATH at compute-node Python startup.

PBS job-state reference (``qstat -f`` → ``job_state``):
    R  — Running
    Q  — Queued (waiting for resources)
    H  — Held
    S  — Suspended
    E  — Exiting (job script finalising)
    C  — Completed (terminal)
    F  — Finished (OpenPBS — terminal, treated as Completed)
    W  — Waiting (deferred execution)

Unit tests use mocked subprocess so they pass without a live PBS cluster.
Live-cluster validation is a separate acceptance gate (see ROADMAP).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from novafabric.runners._poll import jittered_sleep
from novafabric.runners._sitecustomize import HOOK_LOADER as _HOOK_LOADER
from novafabric.runners._types import RunnerJobResult, RunnerJobSpec

# PBS qstat -f job_state values that are terminal.
_PBS_TERMINAL_STATES = {"C", "F"}  # Completed / Finished (OpenPBS)
_PBS_SUCCESS_STATES = {"C", "F"}   # Both map to success in PBS semantics.

# qsub output: "12345.hostname" or plain "12345"
_QSUB_JOBID_RE = re.compile(r"(\d+(?:\.\S+)?)")

# qstat -f field: "    job_state = R"
_QSTAT_STATE_RE = re.compile(r"job_state\s*=\s*(\S+)")
_QSTAT_EXIT_RE = re.compile(r"exit_status\s*=\s*(\S+)")

#: PBS job script template.  All placeholders filled by PBSRunner._build_script().
_PBS_SCRIPT_TEMPLATE = """\
#!/bin/bash
#PBS -N {job_name}
#PBS -l nodes={nodes}:ppn={ppn}
#PBS -l walltime={walltime}
#PBS -q {queue}
#PBS -o {log_dir}/stdout.log
#PBS -e {log_dir}/stderr.log
#PBS -v PYTHONPATH,NOVA_CAPSULE_DIR,NOVA_RUN_ID

cd "$PBS_O_WORKDIR"

# NovaFabric sitecustomize injection — enables wire-level capture on compute node
export PYTHONPATH={sitecustomize_dir}:$PYTHONPATH
export NOVAFABRIC_CAPSULE_DIR={capsule_dir}
export NOVAFABRIC_SPAN_ID={span_id}

{command}
"""


class PBSRunner:
    """Run the captured workload as a PBS Torque / OpenPBS batch job.

    Required ``runner_options``:

    - ``queue`` (str) — PBS queue to submit to (``-q``). Default: ``"default"``.

    Optional ``runner_options``:

    - ``nodes`` (int) — number of nodes (``-l nodes=N``). Default 1.
    - ``ppn`` (int) — processors per node (``-l nodes=N:ppn=M``). Default 1.
    - ``walltime`` (str) — wall-clock limit in ``HH:MM:SS`` format (``-l walltime=``).
      Default ``"01:00:00"``. If ``spec.timeout_s`` is set and ``walltime`` is
      not explicitly provided, the timeout is converted to HH:MM:SS automatically.
    - ``job_name`` (str) — PBS job name (``-N``). Default: ``"nova-{run_id[:8]}"``.
    - ``poll_interval_s`` (float) — qstat poll interval in seconds. Default 30.0.
    - ``output_dir`` (str) — directory for stdout/stderr logs. Default: capsule dir.

    **Required preconditions** (not verified — the job will fail loudly if wrong):
    - ``capsule_dir`` must be on a shared filesystem visible to every PBS compute node.
    - The compute node environment must have ``novafabric`` importable (same venv
      or a container that includes it).
    """

    name: str = "pbs"
    description: str = (
        "Run the captured workload as a PBS Torque / OpenPBS batch job. "
        "Requires a shared filesystem between submit node and compute nodes."
    )

    def __init__(
        self,
        *,
        qsub_bin: str = "qsub",
        qstat_bin: str = "qstat",
        qdel_bin: str = "qdel",
    ) -> None:
        self._qsub = qsub_bin
        self._qstat = qstat_bin
        self._qdel = qdel_bin

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def supports(self) -> tuple[bool, str]:
        """Return ``(True, "")`` if PBS tools are available, else ``(False, reason)``."""
        for tool in (self._qsub, self._qstat, self._qdel):
            if shutil.which(tool) is None:
                return False, f"`{tool}` binary not found on PATH"
        # A quick version probe to confirm qsub is functional.
        try:
            proc = subprocess.run(
                [self._qsub, "--version"],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"could not run `{self._qsub} --version`: {exc}"
        # Some PBS variants exit non-zero for --version but still print usable output.
        # We accept exit 0 or 1 as long as the binary ran.
        if proc.returncode not in (0, 1):
            return False, f"`{self._qsub}` returned unexpected exit code {proc.returncode}"
        return True, ""

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        opts = spec.runner_options or {}
        queue: str = str(opts.get("queue", "default")) if opts.get("queue") else "default"
        nodes: int = int(opts.get("nodes", 1)) if opts.get("nodes") else 1
        ppn: int = int(opts.get("ppn", 1)) if opts.get("ppn") else 1

        # Walltime: explicit option wins; then derive from spec.timeout_s.
        walltime_opt = opts.get("walltime")
        if isinstance(walltime_opt, str) and walltime_opt:
            walltime = walltime_opt
        elif spec.timeout_s is not None:
            walltime = _seconds_to_hms(int(spec.timeout_s))
        else:
            walltime = "01:00:00"

        job_name_opt = opts.get("job_name")
        job_name = (
            str(job_name_opt)
            if isinstance(job_name_opt, str) and job_name_opt
            else f"nova-{spec.run_id[:8]}"
        )

        output_dir_opt = opts.get("output_dir")
        output_dir = (
            Path(output_dir_opt)
            if isinstance(output_dir_opt, str) and output_dir_opt
            else spec.capsule_dir
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        poll_interval = opts.get("poll_interval_s", 30.0)
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            poll_interval = 30.0

        # 1. Materialize sitecustomize.py in the capsule dir (shared FS).
        try:
            (spec.capsule_dir / "sitecustomize.py").write_text(_HOOK_LOADER)
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                runner_error=(
                    f"Could not write sitecustomize.py to {spec.capsule_dir}: {exc}. "
                    "Capsule directory must be on a writable shared filesystem."
                ),
            )

        # 2. Build and write the PBS job script.
        command_str = " ".join(_shell_quote(a) for a in spec.command)
        span_id = spec.env.get("NOVAFABRIC_SPAN_ID", "0" * 16)
        script_content = _PBS_SCRIPT_TEMPLATE.format(
            job_name=job_name,
            nodes=nodes,
            ppn=ppn,
            walltime=walltime,
            queue=queue,
            log_dir=str(output_dir),
            sitecustomize_dir=str(spec.capsule_dir),
            capsule_dir=str(spec.capsule_dir),
            span_id=span_id,
            command=command_str,
        )

        script_path = spec.capsule_dir / f"nova-{spec.run_id[:8]}.pbs"
        try:
            script_path.write_text(script_content)
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                runner_error=f"Could not write PBS job script to {script_path}: {exc}",
            )

        # 3. Submit via qsub.
        try:
            sub = subprocess.run(
                [self._qsub, str(script_path)],
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                stderr=(exc.stderr or b""),
                runner_error=f"`qsub` timed out: {exc}",
            )
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                runner_error=f"PBS (qsub) not available on this system: {exc}",
            )

        if sub.returncode != 0:
            return RunnerJobResult(
                exit_code=125,
                runner_status="failed_setup",
                stdout=sub.stdout,
                stderr=sub.stderr,
                runner_error="qsub submission failed; see stderr",
            )

        job_id = self._parse_job_id(sub.stdout.decode(errors="replace").strip())
        if not job_id:
            return RunnerJobResult(
                exit_code=125,
                runner_status="failed_setup",
                stdout=sub.stdout,
                stderr=sub.stderr,
                runner_error="could not parse job ID from qsub output",
            )

        # 4. Poll qstat -f until terminal state.
        timeout = spec.timeout_s if spec.timeout_s is not None else 3600.0
        deadline = time.monotonic() + timeout
        last_state = "Q"
        exit_status_str = ""

        while time.monotonic() < deadline:
            state, exit_status_candidate = self._query_state(job_id)
            if state is not None:
                last_state = state
                if exit_status_candidate:
                    exit_status_str = exit_status_candidate
            if last_state in _PBS_TERMINAL_STATES:
                break
            jittered_sleep(poll_interval)

        # 5. Read stdout/stderr from the log files PBS wrote.
        stdout_path = output_dir / "stdout.log"
        stderr_path = output_dir / "stderr.log"
        stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
        stderr_bytes = stderr_path.read_bytes() if stderr_path.exists() else b""

        runner_metadata: dict[str, Any] = {
            "job_id": job_id,
            "queue": queue,
            "final_state": last_state,
            "pbs_exit_status": exit_status_str,
        }

        if last_state not in _PBS_TERMINAL_STATES:
            return RunnerJobResult(
                exit_code=124,
                runner_status="timeout",
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                runner_error=(
                    f"qstat still reports state {last_state!r} after {timeout}s"
                ),
                runner_metadata=runner_metadata,
            )

        # Parse workload exit code.
        workload_exit = 0
        if exit_status_str:
            try:
                workload_exit = int(exit_status_str)
            except ValueError:
                workload_exit = 0 if last_state in _PBS_SUCCESS_STATES else 1
        elif last_state not in _PBS_SUCCESS_STATES:
            workload_exit = 1

        return RunnerJobResult(
            exit_code=workload_exit,
            runner_status="completed",
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            runner_metadata=runner_metadata,
        )

    def cancel(self, job_id: str) -> None:
        """Cancel a running PBS job via ``qdel``.

        Raises :class:`subprocess.CalledProcessError` if qdel fails.
        """
        subprocess.run(
            [self._qdel, job_id],
            capture_output=True,
            check=True,
            timeout=15,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_job_id(self, qsub_output: str) -> str:
        """Extract the PBS job ID from qsub stdout.

        PBS prints the job ID on a single line, e.g.:
            ``12345.headnode.example.com``
        or simply ``12345``.  We return the full token as-is so callers
        can pass it unchanged to qstat/qdel.
        """
        # qsub output may have trailing whitespace; clean up.
        token = qsub_output.strip()
        if not token:
            return ""
        m = _QSUB_JOBID_RE.match(token)
        return m.group(1) if m else ""

    def _query_state(self, job_id: str) -> tuple[str | None, str]:
        """Call ``qstat -f <job_id>`` and parse job_state / exit_status.

        Returns ``(state, exit_status_str)`` where ``state`` is one of
        the single-char PBS job state codes (R, Q, C, E, H, S, F, W) or
        ``None`` if qstat failed / did not recognise the job.
        """
        try:
            proc = subprocess.run(
                [self._qstat, "-f", job_id],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None, ""

        if proc.returncode != 0:
            return None, ""

        output = proc.stdout.decode(errors="replace")
        return self._parse_status(output)

    def _parse_status(self, qstat_output: str) -> tuple[str | None, str]:
        """Parse ``qstat -f`` output and return ``(state, exit_status)``."""
        state: str | None = None
        exit_status = ""

        m_state = _QSTAT_STATE_RE.search(qstat_output)
        if m_state:
            state = m_state.group(1).strip()

        m_exit = _QSTAT_EXIT_RE.search(qstat_output)
        if m_exit:
            exit_status = m_exit.group(1).strip()

        return state, exit_status


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _shell_quote(arg: str) -> str:
    """Minimal POSIX-shell quoting (mirrors SlurmRunner helper)."""
    if not arg or any(c in arg for c in " \t\n'\""):
        return "'" + arg.replace("'", "'\"'\"'") + "'"
    return arg


def _seconds_to_hms(total_seconds: int) -> str:
    """Convert seconds to ``HH:MM:SS`` walltime format used by PBS."""
    hours, remainder = divmod(max(0, total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
