"""LSF/PBS evidence is this job's own, and PBS sees an OpenPBS job finish.

Review capture-runners-18: with a shared ``output_dir`` an LSF job whose own log was
never written fell back to "any ``stdout.*.log``" — another job's — and every PBS job
wrote the same ``stdout.log``, so one run's output was sealed into another run's
capsule. Review capture-runners-19: OpenPBS shows a finished job only with
``qstat -x``; ``qstat -f`` errors, so the runner polled a completed job to its
deadline and reported a timeout. Found alongside: OpenPBS prints ``Exit_status``
(capital E), which the exit-status parser never matched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from novafabric.runners import LSFRunner, PBSRunner, RunnerJobSpec

RUN_ID = "01TESTLOGS0000000000000000A"


def _spec(tmp_path: Path, **options: object) -> RunnerJobSpec:
    cap = tmp_path / "capsule"
    cap.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id=RUN_ID,
        command=["python", "agent.py"],
        capsule_dir=cap,
        env={"NOVAFABRIC_CAPSULE_DIR": str(cap), "PATH": "/usr/bin"},
        timeout_s=2.0,
        runner_options={"poll_interval_s": 0.01, **options},
    )


def _done(argv: list[str], out: bytes, rc: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(argv, rc, out, b"" if rc == 0 else b"qstat: error")


# ------------------------------------------------------------------ LSF


def test_lsf_never_reads_another_jobs_log(tmp_path: Path) -> None:
    shared = tmp_path / "shared-logs"
    shared.mkdir()
    (shared / "stdout.999.log").write_text("ANOTHER JOB'S OUTPUT\n")
    (shared / "stderr.999.log").write_text("ANOTHER JOB'S ERRORS\n")

    def fake(argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        if Path(argv[0]).name == "bsub":
            return _done(argv, b"Job <1000> is submitted to queue <normal>.\n")
        return _done(argv, b"1000 user DONE normal host host job Oct 24 10:00\n")

    with patch("subprocess.run", side_effect=fake), patch("time.sleep"):
        result = LSFRunner().run(_spec(tmp_path, output_dir=str(shared)))
    assert b"ANOTHER JOB" not in result.stdout
    assert b"ANOTHER JOB" not in result.stderr


# ------------------------------------------------------------------ PBS


class _PBS:
    def __init__(self, qstat: list[tuple[list[str], bytes, int]]) -> None:
        self.qstat = qstat  # (argv[1:] expected, reply, rc)
        self.calls: list[list[str]] = []
        self.script = ""

    def __call__(self, argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = Path(argv[0]).name
        if tool == "qsub":
            self.script = Path(argv[-1]).read_text()
            return _done(argv, b"88.server\n")
        for args, reply, rc in self.qstat:
            if argv[1:] == args:
                return _done(argv, reply, rc)
        return _done(argv, b"", 1)


FINISHED_OPENPBS = b"Job Id: 88.server\n    job_state = F\n    Exit_status = 3\n"


def test_pbs_logs_are_named_for_this_run(tmp_path: Path) -> None:
    shared = tmp_path / "shared-logs"
    shared.mkdir()
    (shared / "stdout.log").write_text("ANOTHER RUN'S OUTPUT\n")
    fake = _PBS([(["-f", "88.server"], FINISHED_OPENPBS, 0)])
    with patch("subprocess.run", side_effect=fake), patch("time.sleep"):
        result = PBSRunner().run(_spec(tmp_path, output_dir=str(shared)))
    assert f"#PBS -o {shared}/stdout.{RUN_ID}.log" in fake.script
    assert f"#PBS -e {shared}/stderr.{RUN_ID}.log" in fake.script
    assert b"ANOTHER RUN" not in result.stdout


def test_pbs_reads_its_own_log(tmp_path: Path) -> None:
    fake = _PBS([(["-f", "88.server"], FINISHED_OPENPBS, 0)])
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / f"stdout.{RUN_ID}.log").write_text("hello from the job\n")
    with patch("subprocess.run", side_effect=fake), patch("time.sleep"):
        result = PBSRunner().run(_spec(tmp_path))
    assert result.stdout == b"hello from the job\n"


def test_openpbs_finished_job_is_seen_through_qstat_x(tmp_path: Path) -> None:
    fake = _PBS([
        (["-f", "88.server"], b"", 35),  # "Job has finished, use -x or -H"
        (["-x", "-f", "88.server"], FINISHED_OPENPBS, 0),
    ])
    with patch("subprocess.run", side_effect=fake), patch("time.sleep"):
        result = PBSRunner().run(_spec(tmp_path))
    assert result.runner_status == "completed", result.runner_error
    assert result.exit_code == 3  # OpenPBS's capitalised Exit_status is read
    assert result.runner_metadata["final_state"] == "F"


def test_torque_xml_from_qstat_x_is_parsed(tmp_path: Path) -> None:
    xml = (
        b"<Data><Job><Job_Id>88.server</Job_Id><job_state>C</job_state>"
        b"<exit_status>0</exit_status></Job></Data>"
    )
    fake = _PBS([(["-f", "88.server"], b"", 153), (["-x", "-f", "88.server"], xml, 0)])
    with patch("subprocess.run", side_effect=fake), patch("time.sleep"):
        result = PBSRunner().run(_spec(tmp_path))
    assert result.runner_status == "completed", result.runner_error
    assert result.exit_code == 0
