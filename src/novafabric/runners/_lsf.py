"""IBM LSF (Load Sharing Facility) runner (ADR-0025 Runners.6).

Runs the captured workload as an LSF batch job via ``bsub``, polls
``bjobs`` for completion, and reads stdout/stderr from the job's
output files. The capsule directory MUST be on a filesystem shared
between the submit node and the compute node — typically the cluster's
home or scratch filesystem (NFS, GPFS, Spectrum Scale, etc.). The
runner does NOT rsync artifacts; it relies entirely on the shared FS.

This implementation follows the same pattern as :class:`SlurmRunner`:
1. Write an LSF job script to a temporary file inside the capsule dir.
2. Submit via ``bsub < script.lsf`` and parse the returned job ID.
3. Poll ``bjobs -noheader <job_id>`` until the job enters a terminal state.
4. Read per-job stdout/stderr log files written by LSF.

Wire-level capture is enabled by the same sitecustomize injection used
by SlurmRunner: ``sitecustomize.py`` is materialized in the capsule dir
(shared FS) and picked up via PYTHONPATH at compute-node Python startup.

LSF job-state reference (``bjobs`` STAT column):
    RUN    — Currently running.
    PEND   — Pending (waiting for resources).
    DONE   — Exited with exit code 0. Terminal — success.
    EXIT   — Exited with non-zero exit code. Terminal — failure.
    USUSP  — Suspended by user.
    SSUSP  — Suspended by system.
    PSUSP  — Pending and suspended.
    WAIT   — Waiting for a pre-execution command to complete.
    ZOMBI  — Zombie job (should not occur in normal operation).
    UNKWN  — Unknown state (bjobs cannot communicate with sbatchd).

Unit tests use mocked subprocess so they pass without a live LSF cluster.
Live-cluster validation is a separate acceptance gate (see ROADMAP).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from novafabric.runners._sitecustomize import HOOK_LOADER as _HOOK_LOADER
from novafabric.runners._types import RunnerJobResult, RunnerJobSpec

# LSF bjobs STAT values that indicate terminal states.
_LSF_TERMINAL_STATES = {"DONE", "EXIT", "ZOMBI"}
_LSF_SUCCESS_STATES = {"DONE"}

# bsub prints: "Job <12345> is submitted to queue <normal>."
_BSUB_JOBID_RE = re.compile(r"Job\s+<(\d+)>")

#: LSF job script template. All placeholders filled by LSFRunner.run().
_LSF_SCRIPT_TEMPLATE = """\
#!/bin/bash
#BSUB -J {job_name}
#BSUB -n {n_cores}
#BSUB -W {walltime}
#BSUB -q {queue}
#BSUB -o {log_dir}/stdout.%J.log
#BSUB -e {log_dir}/stderr.%J.log
{resource_line}
# NovaFabric sitecustomize injection — enables wire-level capture on compute node
export PYTHONPATH={sitecustomize_dir}:$PYTHONPATH
export NOVAFABRIC_CAPSULE_DIR={capsule_dir}
export NOVAFABRIC_SPAN_ID={span_id}

{command}
"""


class LSFRunner:
    """Run the captured workload as an IBM LSF batch job.

    Required ``runner_options``:

    - ``queue`` (str) — LSF queue to submit to (``-q``). Default: ``"normal"``.

    Optional ``runner_options``:

    - ``n_cores`` (int) — number of processor slots (``-n``). Default 1.
    - ``walltime`` (str) — wall-clock limit in ``H:MM`` format (``-W``).
      Default ``"1:00"``. If ``spec.timeout_s`` is set and ``walltime`` is
      not explicitly provided, the timeout is converted to ``H:MM`` automatically.
    - ``job_name`` (str) — LSF job name (``-J``). Default: ``"nova-{run_id[:8]}"``.
    - ``resource_requirement`` (str | None) — LSF resource requirement string
      (``-R``), e.g. ``"rusage[mem=4096]"``. Default: not set.
    - ``poll_interval_s`` (float) — bjobs poll interval in seconds. Default 30.0.
    - ``output_dir`` (str) — directory for stdout/stderr logs. Default: capsule dir.

    **Required preconditions** (not verified — the job will fail loudly if wrong):
    - ``capsule_dir`` must be on a shared filesystem visible to every LSF compute host.
    - The compute host environment must have ``novafabric`` importable (same venv
      or a container that includes it).
    """

    name: str = "lsf"
    description: str = (
        "Run the captured workload as an IBM LSF batch job. "
        "Requires a shared filesystem between submit host and compute hosts."
    )

    def __init__(
        self,
        *,
        bsub_bin: str = "bsub",
        bjobs_bin: str = "bjobs",
        bkill_bin: str = "bkill",
    ) -> None:
        self._bsub = bsub_bin
        self._bjobs = bjobs_bin
        self._bkill = bkill_bin

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def supports(self) -> tuple[bool, str]:
        """Return ``(True, "")`` if LSF tools are available, else ``(False, reason)``."""
        for tool in (self._bsub, self._bjobs, self._bkill):
            if shutil.which(tool) is None:
                return False, f"`{tool}` binary not found on PATH"
        # Quick probe: bsub -h exits 0 or 1 on all known LSF versions.
        try:
            proc = subprocess.run(
                [self._bsub, "-V"],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"could not run `{self._bsub} -V`: {exc}"
        if proc.returncode not in (0, 1):
            return False, f"`{self._bsub}` returned unexpected exit code {proc.returncode}"
        return True, ""

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        opts = spec.runner_options or {}
        queue: str = str(opts.get("queue", "normal")) if opts.get("queue") else "normal"
        n_cores: int = int(opts.get("n_cores", 1)) if opts.get("n_cores") else 1

        # Walltime: explicit option wins; then derive from spec.timeout_s.
        walltime_opt = opts.get("walltime")
        if isinstance(walltime_opt, str) and walltime_opt:
            walltime = walltime_opt
        elif spec.timeout_s is not None:
            walltime = _seconds_to_lsf_walltime(int(spec.timeout_s))
        else:
            walltime = "1:00"

        job_name_opt = opts.get("job_name")
        job_name = (
            str(job_name_opt)
            if isinstance(job_name_opt, str) and job_name_opt
            else f"nova-{spec.run_id[:8]}"
        )

        resource_req_opt = opts.get("resource_requirement")
        resource_line = (
            f"#BSUB -R {resource_req_opt}"
            if isinstance(resource_req_opt, str) and resource_req_opt
            else ""
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

        # 2. Build and write the LSF job script.
        command_str = " ".join(_shell_quote(a) for a in spec.command)
        span_id = spec.env.get("NOVAFABRIC_SPAN_ID", "0" * 16)
        script_content = _LSF_SCRIPT_TEMPLATE.format(
            job_name=job_name,
            n_cores=n_cores,
            walltime=walltime,
            queue=queue,
            log_dir=str(output_dir),
            resource_line=resource_line,
            sitecustomize_dir=str(spec.capsule_dir),
            capsule_dir=str(spec.capsule_dir),
            span_id=span_id,
            command=command_str,
        )

        script_path = spec.capsule_dir / f"nova-{spec.run_id[:8]}.lsf"
        try:
            script_path.write_text(script_content)
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                runner_error=f"Could not write LSF job script to {script_path}: {exc}",
            )

        # 3. Submit via bsub with the script piped on stdin (LSF convention).
        try:
            with script_path.open("rb") as script_fh:
                sub = subprocess.run(
                    [self._bsub],
                    stdin=script_fh,
                    capture_output=True,
                    timeout=30,
                )
        except subprocess.TimeoutExpired as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                stderr=(exc.stderr or b""),
                runner_error=f"`bsub` timed out: {exc}",
            )
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                runner_error=f"LSF (bsub) not available on this system: {exc}",
            )

        if sub.returncode != 0:
            return RunnerJobResult(
                exit_code=125,
                runner_status="failed_setup",
                stdout=sub.stdout,
                stderr=sub.stderr,
                runner_error="bsub submission failed; see stderr",
            )

        job_id = self._parse_job_id(sub.stdout.decode(errors="replace"))
        if not job_id:
            return RunnerJobResult(
                exit_code=125,
                runner_status="failed_setup",
                stdout=sub.stdout,
                stderr=sub.stderr,
                runner_error="could not parse job ID from bsub output",
            )

        # 4. Poll bjobs until terminal state.
        timeout = spec.timeout_s if spec.timeout_s is not None else 3600.0
        deadline = time.monotonic() + timeout
        last_state = "PEND"

        while time.monotonic() < deadline:
            state = self._query_state(job_id)
            if state is not None:
                last_state = state
            if last_state in _LSF_TERMINAL_STATES:
                break
            time.sleep(poll_interval)

        # 5. Read stdout/stderr from the log files LSF wrote.
        # LSF appends %J (job ID) to the log filenames.
        stdout_bytes, stderr_bytes = self._read_log_files(output_dir, job_id)

        runner_metadata: dict[str, Any] = {
            "job_id": job_id,
            "queue": queue,
            "final_state": last_state,
        }

        if last_state not in _LSF_TERMINAL_STATES:
            return RunnerJobResult(
                exit_code=124,
                runner_status="timeout",
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                runner_error=(
                    f"bjobs still reports state {last_state!r} after {timeout}s"
                ),
                runner_metadata=runner_metadata,
            )

        workload_exit = 0 if last_state in _LSF_SUCCESS_STATES else 1

        return RunnerJobResult(
            exit_code=workload_exit,
            runner_status="completed",
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            runner_metadata=runner_metadata,
        )

    def cancel(self, job_id: str) -> None:
        """Cancel a running LSF job via ``bkill``.

        Raises :class:`subprocess.CalledProcessError` if bkill fails.
        """
        subprocess.run(
            [self._bkill, job_id],
            capture_output=True,
            check=True,
            timeout=15,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_job_id(self, bsub_output: str) -> str:
        """Extract the numeric job ID from bsub stdout.

        bsub prints: ``Job <12345> is submitted to queue <normal>.``
        We return the numeric job ID as a string (e.g. ``"12345"``).
        """
        m = _BSUB_JOBID_RE.search(bsub_output)
        if not m:
            return ""
        return m.group(1)

    def _query_state(self, job_id: str) -> str | None:
        """Call ``bjobs -noheader <job_id>`` and return the STAT column value.

        Returns ``None`` if bjobs fails or the job is not found.
        The STAT column is the third whitespace-delimited field in the
        ``bjobs -noheader`` output line:
            ``JOBID USER STAT QUEUE FROM_HOST EXEC_HOST JOB_NAME SUBMIT_TIME``
        """
        try:
            proc = subprocess.run(
                [self._bjobs, "-noheader", job_id],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        if proc.returncode != 0:
            return None

        return self._parse_status(proc.stdout.decode(errors="replace"))

    def _parse_status(self, bjobs_output: str) -> str | None:
        """Parse the STAT field from ``bjobs -noheader`` output.

        ``bjobs -noheader`` output columns (space-separated, variable width):
            JOBID  USER  STAT  QUEUE  FROM_HOST  EXEC_HOST  JOB_NAME  SUBMIT_TIME

        The STAT field is the third token on the first non-empty line.
        """
        for line in bjobs_output.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                return parts[2]
        return None

    def _read_log_files(
        self, output_dir: Path, job_id: str
    ) -> tuple[bytes, bytes]:
        """Read LSF stdout/stderr log files for the given job ID.

        LSF names the files using ``%J`` substitution; the actual filenames
        are ``stdout.<jobid>.log`` and ``stderr.<jobid>.log``.  We search
        for matching files with a glob in case the naming convention varies.
        """
        stdout_bytes = b""
        stderr_bytes = b""

        # Direct hit: {output_dir}/stdout.<job_id>.log
        stdout_direct = output_dir / f"stdout.{job_id}.log"
        if stdout_direct.exists():
            stdout_bytes = stdout_direct.read_bytes()
        else:
            # Fallback: any file matching stdout.*.log in the output dir
            candidates = sorted(output_dir.glob("stdout.*.log"))
            if candidates:
                stdout_bytes = candidates[-1].read_bytes()

        stderr_direct = output_dir / f"stderr.{job_id}.log"
        if stderr_direct.exists():
            stderr_bytes = stderr_direct.read_bytes()
        else:
            candidates = sorted(output_dir.glob("stderr.*.log"))
            if candidates:
                stderr_bytes = candidates[-1].read_bytes()

        return stdout_bytes, stderr_bytes


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _shell_quote(arg: str) -> str:
    """Minimal POSIX-shell quoting (mirrors SlurmRunner helper)."""
    if not arg or any(c in arg for c in " \t\n'\""):
        return "'" + arg.replace("'", "'\"'\"'") + "'"
    return arg


def _seconds_to_lsf_walltime(total_seconds: int) -> str:
    """Convert seconds to ``H:MM`` walltime format used by LSF ``-W``."""
    total_minutes = max(1, (total_seconds + 59) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}"
