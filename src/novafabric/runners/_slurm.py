"""SLURM runner (ADR-0025 Runners.4).

Runs the captured workload via ``sbatch`` on a SLURM cluster, polls
``sacct`` for completion, and reads stdout/stderr from the job's
output files. Capsule directory MUST be on a filesystem shared between
the submit node and the compute node — typically the cluster's home or
scratch (``/scratch``, ``/lustre/...``, etc.). The runner does NOT
rsync artifacts; we trust the shared FS.

This v0.6.1 first cut does the minimum to satisfy the protocol on a
real cluster:
- Submit a one-line sbatch script (``--wrap=...``)
- Poll ``sacct`` for state
- Read the per-job ``slurm-<jobid>.out`` / ``.err`` files

Validated against at least 2 distinct cluster configurations is queued
as a v0.6.x acceptance gate (per ROADMAP and ADR-0025) — for now, the
unit tests use mocked subprocess so this lands without a live cluster
in CI.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from novafabric.runners._types import RunnerJobResult, RunnerJobSpec

logger = logging.getLogger(__name__)

# sacct end states. JobState column values that mean "done, succeeded".
# https://slurm.schedmd.com/sacct.html#lbAJ
_SACCT_SUCCESS_STATES = {"COMPLETED"}
# All terminal states (success, failure, cancelled, timeout, etc.)
_SACCT_TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED",
}

# sbatch prints "Submitted batch job 12345" on success; capture the id.
_SBATCH_JOBID_RE = re.compile(r"Submitted batch job (\d+)")


def _shell_quote(arg: str) -> str:
    """Minimal POSIX-shell quoting for the --wrap script."""
    if not arg or any(c in arg for c in " \t\n'\""):
        return "'" + arg.replace("'", "'\"'\"'") + "'"
    return arg


def _build_wrap_script(
    command: list[str], env: dict[str, str], capsule_dir: Path
) -> str:
    """Build the inline shell script for sbatch --wrap.

    Exports the orchestrator-provided env vars, prepends the capsule
    directory (which contains ``sitecustomize.py``) to ``PYTHONPATH``
    so the compute node's Python auto-installs NovaFabric's hooks at
    startup, and execs the user's command.

    The ``PYTHONPATH`` injection is what makes wire-level capture
    actually fire on the compute node — without it, the workload runs
    bare and ``model-calls.jsonl`` ends up empty regardless of what
    the workload does. (Surfaced by the v0.6.11 live e2e test on a
    real cluster.)
    """
    lines: list[str] = ["set -e"]
    for k, v in env.items():
        if k.startswith("NOVAFABRIC_") or k in ("PATH",):
            lines.append(f"export {k}={_shell_quote(v)}")
    # Make sure NOVAFABRIC_CAPSULE_DIR points at the (shared-FS) path.
    lines.append(f"export NOVAFABRIC_CAPSULE_DIR={_shell_quote(str(capsule_dir))}")
    # Prepend the capsule dir to PYTHONPATH so sitecustomize.py
    # (materialized there by SlurmRunner.run()) auto-loads. Preserve
    # any existing PYTHONPATH the orchestrator passed through.
    existing_pythonpath = env.get("PYTHONPATH", "")
    new_pythonpath = (
        f"{capsule_dir}:{existing_pythonpath}"
        if existing_pythonpath else str(capsule_dir)
    )
    lines.append(f"export PYTHONPATH={_shell_quote(new_pythonpath)}")
    lines.append("exec " + " ".join(_shell_quote(a) for a in command))
    return "\n".join(lines)


class SlurmRunner:
    """Run the captured workload as a SLURM batch job.

    Required ``runner_options``:

    - ``partition`` (str) — SLURM partition (queue) to submit to.

    Optional ``runner_options``:

    - ``account`` (str) — SLURM account / project (``--account``).
    - ``qos`` (str) — quality-of-service (``--qos``).
    - ``time`` (str) — wall-clock limit, e.g. ``"00:30:00"`` (``--time``).
      If unset, derived from ``spec.timeout_s`` (or SLURM partition default).
    - ``nodes`` (int) — number of nodes (``--nodes``). Default 1.
    - ``gres`` (str) — generic resources, e.g. ``"gpu:a100:1"`` (``--gres``).
    - ``mem`` (str) — memory request, e.g. ``"4G"`` (``--mem``).
    - ``constraint`` (str) — node feature constraint (``--constraint``).
    - ``poll_interval_s`` (float) — sacct poll interval. Default 5.0.
    - ``output_dir`` (str) — where to write slurm-<jobid>.out/.err.
      Default: capsule dir.

    **Required preconditions** (the runner does NOT verify these — the
    job will fail loudly if they are wrong):
    - ``capsule_dir`` is on a filesystem shared between the submit node
      and any compute node SLURM might allocate.
    - The image / environment on the compute node has ``novafabric``
      importable (typically by activating the same venv the submitter
      uses, or by using a container runner like ``apptainer``).
    """

    @staticmethod
    def _parse_scontrol_show_job(output: str) -> tuple[str | None, str]:
        """Parse the relevant fields from ``scontrol show job <jobid>``.

        scontrol output is space-separated key=value pairs (sometimes
        wrapped across lines). We only care about ``JobState`` and
        ``ExitCode``; everything else is ignored.

        Returns ``(state, exit_code_str)`` or ``(None, "")`` if the
        output doesn't contain a JobState (e.g., "Invalid job id").
        """
        state: str | None = None
        exit_code = ""
        for token in output.split():
            if token.startswith("JobState="):
                state = token[len("JobState="):]
            elif token.startswith("ExitCode="):
                exit_code = token[len("ExitCode="):]
        return state, exit_code

    def _query_state(self, jobid: str) -> tuple[str | None, str]:
        """Query the current state of ``jobid``.

        Tries ``scontrol show job`` first (fast, slurmdbd-independent),
        falls back to ``sacct`` (durable, needs slurmdbd). Returns
        ``(state, exit_code_str)``; either may be empty if neither
        tool is currently usable for this job.
        """
        # Primary: scontrol (works without slurmdbd; fast)
        try:
            sc = subprocess.run(
                [self._scontrol, "show", "job", jobid],
                capture_output=True, timeout=10,
            )
            if sc.returncode == 0:
                state, exit_code = self._parse_scontrol_show_job(
                    sc.stdout.decode(errors="replace")
                )
                if state is not None:
                    return state, exit_code
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Fallback: sacct (needs slurmdbd; durable)
        try:
            sacct = subprocess.run(
                [self._sacct, "-j", jobid,
                 "--format=JobID,State,ExitCode",
                 "--noheader", "--parsable2"],
                capture_output=True, timeout=10,
            )
            if sacct.returncode == 0:
                lines = [
                    line for line
                    in sacct.stdout.decode(errors="replace").splitlines()
                    if line.strip()
                ]
                if lines:
                    parts = lines[0].split("|")
                    if len(parts) >= 2:
                        state = parts[1].split()[0]  # strip trailing flags
                        exit_code = parts[2] if len(parts) >= 3 else ""
                        return state, exit_code
        except (subprocess.TimeoutExpired, OSError):
            pass

        return None, ""

    name: str = "slurm"
    description: str = (
        "Run the captured workload as a SLURM batch job. "
        "Requires runner_options['partition'] and a shared filesystem."
    )

    def __init__(
        self,
        *,
        sbatch_bin: str = "sbatch",
        sacct_bin: str = "sacct",
        scontrol_bin: str = "scontrol",
    ) -> None:
        self._sbatch = sbatch_bin
        self._sacct = sacct_bin
        # scontrol added in v0.6.10 (live-cluster validation finding):
        # `sacct` requires slurmdbd, which is NOT installed in many
        # development / minimal SLURM clusters. `scontrol show job
        # <jobid>` reads directly from slurmctld memory and works
        # without slurmdbd. We use scontrol as the primary state
        # source and fall back to sacct (which has the advantage of
        # working long after slurmctld has forgotten the job, ~5 min).
        self._scontrol = scontrol_bin

    def supports(self) -> tuple[bool, str]:
        # sacct is no longer strictly required (scontrol covers state
        # for the recent-job window). But we still probe for it because
        # the fallback path uses it. Same for scontrol.
        for tool in (self._sbatch, self._sacct, self._scontrol):
            if shutil.which(tool) is None:
                return False, f"`{tool}` binary not found on PATH"
        # `sbatch --version` exits 0 if SLURM is properly installed.
        try:
            proc = subprocess.run(
                [self._sbatch, "--version"], capture_output=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"could not run `{self._sbatch} --version`: {exc}"
        if proc.returncode != 0:
            return False, f"`{self._sbatch}` is not usable"
        return True, ""

    def _capture_energy(self, jobid: str, spec: RunnerJobSpec) -> bool:
        """Query ``sacct`` for the job's measured energy and record a receipt.

        Best-effort, fail-open (ADR-0093 §A2): a missing column, an unconfigured
        ``acct_gather_energy``, or any subprocess error simply records no
        receipt and never fails the run. Returns ``True`` iff a receipt was
        written. Factored so the parse/receipt step
        (:func:`capture_slurm_energy`) is unit-tested without a live cluster.
        """
        from novafabric.runners._slurm_energy import (
            SACCT_ENERGY_FORMAT,
            capture_slurm_energy,
        )

        try:
            proc = subprocess.run(
                [self._sacct, "-j", jobid, f"--format={SACCT_ENERGY_FORMAT}",
                 "--parsable2"],
                capture_output=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("sacct energy query failed for job %s: %s", jobid, exc)
            return False
        if proc.returncode != 0:
            return False

        run_id = spec.run_id or jobid
        node_id = spec.env.get("NOVAFABRIC_NODE_ID") or "slurm"
        try:
            receipt = capture_slurm_energy(
                capsule_dir=spec.capsule_dir,
                job_id=jobid,
                sacct_output=proc.stdout.decode(errors="replace"),
                run_id=run_id,
                node_id=node_id,
            )
        except OSError as exc:
            logger.warning("could not write energy receipt for job %s: %s", jobid, exc)
            return False
        return receipt is not None

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        opts = spec.runner_options or {}
        partition = opts.get("partition") if isinstance(opts.get("partition"), str) else None
        if not partition:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                runner_error=(
                    "SlurmRunner requires runner_options['partition']. "
                    "Pass --runner-option partition=<queue>."
                ),
            )

        # v0.6.11: materialize the sitecustomize hook loader into the
        # capsule directory (which is on shared FS and visible to the
        # compute node). The wrap script's PYTHONPATH points at the
        # capsule_dir so Python auto-loads it at interpreter startup
        # and NovaFabric's hooks install before the user's code runs.
        # Without this, wire-level capture silently no-ops on SLURM —
        # the model-calls.jsonl file is created but never written to.
        from novafabric.runners._sitecustomize import HOOK_LOADER
        try:
            (spec.capsule_dir / "sitecustomize.py").write_text(HOOK_LOADER)
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                runner_error=(
                    f"Could not write sitecustomize.py to {spec.capsule_dir}: {exc}. "
                    "Capsule directory must be on a writable shared filesystem."
                ),
            )

        output_dir_opt = opts.get("output_dir")
        output_dir = (
            Path(output_dir_opt) if isinstance(output_dir_opt, str) and output_dir_opt
            else spec.capsule_dir
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build sbatch argv.
        argv: list[str] = [
            self._sbatch,
            f"--partition={partition}",
            "--parsable",  # print just the job id on stdout
            f"--output={output_dir}/slurm-%j.out",
            f"--error={output_dir}/slurm-%j.err",
            f"--chdir={spec.capsule_dir.parent}",
        ]
        for opt_name, sbatch_flag in (
            ("account", "--account"),
            ("qos", "--qos"),
            ("nodes", "--nodes"),
            ("gres", "--gres"),
            ("mem", "--mem"),
            ("constraint", "--constraint"),
        ):
            value = opts.get(opt_name)
            if isinstance(value, (str, int)) and str(value):
                argv.append(f"{sbatch_flag}={value}")

        # Time limit: explicit `time` option, else derive from spec.timeout_s.
        time_opt = opts.get("time")
        if isinstance(time_opt, str) and time_opt:
            argv.append(f"--time={time_opt}")
        elif spec.timeout_s is not None:
            mins = max(1, int(spec.timeout_s // 60))
            argv.append(f"--time={mins}")

        wrap = _build_wrap_script(spec.command, spec.env, spec.capsule_dir)
        argv.extend(["--wrap", wrap])

        # 1. Submit.
        try:
            sub = subprocess.run(argv, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                stderr=(exc.stderr or b""),
                runner_error=f"`sbatch` timed out: {exc}",
            )
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                runner_error=str(exc),
            )
        if sub.returncode != 0:
            return RunnerJobResult(
                exit_code=125, runner_status="failed_setup",
                stdout=sub.stdout, stderr=sub.stderr,
                runner_error="sbatch submission failed; see stderr",
            )

        out = sub.stdout.decode(errors="replace").strip()
        # --parsable returns "<jobid>[;cluster]". A bare integer is also valid.
        jobid = out.split(";", 1)[0].strip()
        if not jobid.isdigit():
            # Fallback to legacy "Submitted batch job <id>" parser.
            m = _SBATCH_JOBID_RE.search(sub.stdout.decode(errors="replace"))
            jobid = m.group(1) if m else ""
        if not jobid:
            return RunnerJobResult(
                exit_code=125, runner_status="failed_setup",
                stdout=sub.stdout, stderr=sub.stderr,
                runner_error="could not parse job id from sbatch output",
            )

        # 2. Poll for terminal state.
        # Strategy (added v0.6.10 after live-cluster validation):
        #   - PRIMARY: `scontrol show job <jobid>` — reads directly from
        #     slurmctld, works without slurmdbd. Returns 'JobState=...'
        #     and 'ExitCode=N:M'. Becomes "Invalid job id" once
        #     slurmctld forgets the job (~5 min after completion).
        #   - FALLBACK: `sacct` — needs slurmdbd. Available indefinitely
        #     when slurmdbd is configured.
        timeout = spec.timeout_s if spec.timeout_s is not None else 3600.0
        poll_interval = opts.get("poll_interval_s", 5.0)
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            poll_interval = 5.0

        deadline = time.monotonic() + timeout
        last_state = "PENDING"
        exit_code_str = ""
        while time.monotonic() < deadline:
            state, exit_code_str_candidate = self._query_state(jobid)
            if state is not None:
                last_state = state
                if exit_code_str_candidate:
                    exit_code_str = exit_code_str_candidate
            if last_state in _SACCT_TERMINAL_STATES:
                break
            time.sleep(poll_interval)

        # 3. Read stdout/stderr files.
        stdout_path = output_dir / f"slurm-{jobid}.out"
        stderr_path = output_dir / f"slurm-{jobid}.err"
        stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
        stderr_bytes = stderr_path.read_bytes() if stderr_path.exists() else b""

        runner_metadata: dict[str, Any] = {
            "job_id": jobid,
            "partition": partition,
            "final_state": last_state,
            "sacct_exit_code": exit_code_str,
        }

        if last_state not in _SACCT_TERMINAL_STATES:
            return RunnerJobResult(
                exit_code=124, runner_status="timeout",
                stdout=stdout_bytes, stderr=stderr_bytes,
                runner_error=(
                    f"sacct still reports state {last_state!r} after {timeout}s"
                ),
                runner_metadata=runner_metadata,
            )

        # Energy receipt (ADR-0093 §A2): on completion, query sacct for the
        # measured per-job ConsumedEnergyRaw and record an sacct_energy receipt
        # into the capsule. Best-effort / fail-open — never breaks the run.
        try:
            if self._capture_energy(jobid, spec):
                runner_metadata["energy_receipt"] = "sacct_energy"
        except Exception:  # noqa: BLE001 — energy capture must never fail the run
            logger.warning("energy capture raised for job %s", jobid, exc_info=True)

        # sacct ExitCode format is "<workload>:<signal>". Use the workload
        # half. If parsing fails, use 0/1 based on the success state.
        workload_exit = 0
        if exit_code_str and ":" in exit_code_str:
            try:
                workload_exit = int(exit_code_str.split(":", 1)[0])
            except ValueError:
                workload_exit = 0 if last_state in _SACCT_SUCCESS_STATES else 1
        elif last_state not in _SACCT_SUCCESS_STATES:
            workload_exit = 1

        return RunnerJobResult(
            exit_code=workload_exit,
            runner_status="completed" if last_state in _SACCT_SUCCESS_STATES else "completed",
            stdout=stdout_bytes, stderr=stderr_bytes,
            runner_metadata=runner_metadata,
        )
