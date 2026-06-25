"""Unit tests for the SlurmRunner (ADR-0025 Runners.4).

Mocks subprocess.run so they pass without sbatch/sacct or a SLURM
cluster. Live-cluster validation against 2+ distinct configs is the
acceptance gate per ROADMAP — queued separately.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.runners import RunnerJobSpec, RunnerSpec, SlurmRunner
from novafabric.runners._slurm import _build_wrap_script, _shell_quote


def _spec(
    tmp_path: Path, command: list[str], **kwargs: object
) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTSLURM00000000000000000",
        command=command,
        capsule_dir=capsule_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
            "PATH": "/usr/bin",
        },
        **kwargs,  # type: ignore[arg-type]
    )


class TestSlurmRunnerProtocol:
    def test_satisfies_runner_spec(self) -> None:
        runner: RunnerSpec = SlurmRunner()
        assert runner.name == "slurm"
        assert "slurm" in runner.description.lower() or "SLURM" in runner.description


class TestSlurmRunnerSupports:
    def test_returns_false_when_sbatch_not_on_path(self) -> None:
        def which(name: str) -> str | None:
            return None if name == "sbatch" else "/usr/bin/sacct"

        with patch("shutil.which", side_effect=which):
            ok, why = SlurmRunner().supports()
        assert ok is False
        assert "sbatch" in why

    def test_returns_false_when_sacct_not_on_path(self) -> None:
        def which(name: str) -> str | None:
            return "/usr/bin/sbatch" if name == "sbatch" else None

        with patch("shutil.which", side_effect=which):
            ok, why = SlurmRunner().supports()
        assert ok is False
        assert "sacct" in why

    def test_returns_true_when_both_present(self) -> None:
        version_ok = subprocess.CompletedProcess([], 0, b"slurm 23.11.6\n", b"")
        with patch("shutil.which", return_value="/usr/bin/sbatch"), \
             patch("subprocess.run", return_value=version_ok):
            ok, why = SlurmRunner().supports()
        assert ok is True
        assert why == ""


class TestSlurmRunnerErrorPaths:
    def test_missing_partition_returns_failed_setup(self, tmp_path: Path) -> None:
        result = SlurmRunner().run(_spec(tmp_path, ["echo"]))
        assert result.exit_code == 127
        assert result.runner_status == "failed_setup"
        assert "partition" in (result.runner_error or "").lower()

    def test_sbatch_failure_returns_failed_setup(self, tmp_path: Path) -> None:
        sbatch_fail = subprocess.CompletedProcess(
            [], 1, b"", b"sbatch: error: Invalid partition specified\n",
        )
        with patch("subprocess.run", return_value=sbatch_fail):
            result = SlurmRunner().run(_spec(
                tmp_path, ["echo"], runner_options={"partition": "no-such"},
            ))
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 125


class TestSlurmRunnerSuccessPath:
    def test_completed_job_returns_workload_exit_code(self, tmp_path: Path) -> None:
        sbatch = subprocess.CompletedProcess([], 0, b"12345\n", b"")
        # v0.6.10: scontrol is the primary state source (works without slurmdbd).
        scontrol = subprocess.CompletedProcess(
            [], 0,
            b"JobId=12345 JobState=COMPLETED ExitCode=0:0 ...",
            b"",
        )
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir(parents=True, exist_ok=True)
        (capsule_dir / "slurm-12345.out").write_bytes(b"hello\n")
        (capsule_dir / "slurm-12345.err").write_bytes(b"")

        spec = RunnerJobSpec(
            run_id="01TESTSLURM00000000000000000",
            command=["echo", "hello"],
            capsule_dir=capsule_dir,
            env={"NOVAFABRIC_SPAN_ID": "0" * 16},
            runner_options={"partition": "gpu"},
        )

        with patch("subprocess.run", side_effect=[sbatch, scontrol]):
            result = SlurmRunner().run(spec)
        assert result.runner_status == "completed"
        assert result.exit_code == 0
        assert result.stdout == b"hello\n"
        assert result.runner_metadata["job_id"] == "12345"
        assert result.runner_metadata["final_state"] == "COMPLETED"

    def test_failed_job_propagates_exit_code(self, tmp_path: Path) -> None:
        sbatch = subprocess.CompletedProcess([], 0, b"99\n", b"")
        scontrol = subprocess.CompletedProcess(
            [], 0,
            b"JobId=99 JobState=FAILED ExitCode=42:0 ...",
            b"",
        )
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir(parents=True, exist_ok=True)

        spec = RunnerJobSpec(
            run_id="01TESTSLURM00000000000000000",
            command=["false"], capsule_dir=capsule_dir,
            env={"NOVAFABRIC_SPAN_ID": "0" * 16},
            runner_options={"partition": "gpu"},
        )

        with patch("subprocess.run", side_effect=[sbatch, scontrol]):
            result = SlurmRunner().run(spec)
        assert result.exit_code == 42
        assert result.runner_metadata["final_state"] == "FAILED"

    def test_legacy_sbatch_output_parsed(self, tmp_path: Path) -> None:
        """When --parsable is not honored (older slurm), sbatch prints
        'Submitted batch job 12345'. The runner should still extract the id."""
        sbatch = subprocess.CompletedProcess(
            [], 0, b"Submitted batch job 12345\n", b"",
        )
        scontrol = subprocess.CompletedProcess(
            [], 0,
            b"JobId=12345 JobState=COMPLETED ExitCode=0:0 ...",
            b"",
        )
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir(parents=True, exist_ok=True)

        spec = RunnerJobSpec(
            run_id="01X", command=["x"], capsule_dir=capsule_dir,
            env={}, runner_options={"partition": "gpu"},
        )
        with patch("subprocess.run", side_effect=[sbatch, scontrol]):
            result = SlurmRunner().run(spec)
        assert result.exit_code == 0
        assert result.runner_metadata["job_id"] == "12345"


class TestSlurmRunnerStateQueryFallback:
    """v0.6.10: scontrol is primary, sacct is fallback.

    These tests cover the new state-query strategy added after live
    cluster validation surfaced that sacct alone fails when slurmdbd
    isn't installed (common in dev/test clusters)."""

    def test_scontrol_used_first_when_available(self, tmp_path: Path) -> None:
        """The defining test for the v0.6.10 fix. scontrol returns
        COMPLETED; sacct is NEVER called."""
        runner = SlurmRunner()
        scontrol_ok = subprocess.CompletedProcess(
            [], 0,
            b"JobId=42 JobState=COMPLETED ExitCode=0:0 ...",
            b"",
        )
        with patch("subprocess.run", return_value=scontrol_ok) as mock_run:
            state, exit_code = runner._query_state("42")
        assert state == "COMPLETED"
        assert exit_code == "0:0"
        # Exactly one subprocess call — scontrol. sacct never invoked.
        assert mock_run.call_count == 1
        assert "scontrol" in str(mock_run.call_args[0][0])

    def test_sacct_fallback_when_scontrol_returns_nonzero(self, tmp_path: Path) -> None:
        """If slurmctld has forgotten the job (typically ~5 min after
        completion), scontrol returns 'Invalid job id' (non-zero exit).
        Fall back to sacct."""
        runner = SlurmRunner()
        scontrol_fail = subprocess.CompletedProcess(
            [], 1, b"", b"slurm_load_jobs error: Invalid job id specified\n",
        )
        sacct_ok = subprocess.CompletedProcess(
            [], 0, b"42|COMPLETED|0:0\n", b"",
        )
        with patch("subprocess.run", side_effect=[scontrol_fail, sacct_ok]):
            state, exit_code = runner._query_state("42")
        assert state == "COMPLETED"
        assert exit_code == "0:0"

    def test_returns_none_when_both_fail(self) -> None:
        """If both scontrol and sacct fail, return (None, '') so the
        polling loop continues. Never raises."""
        runner = SlurmRunner()
        scontrol_fail = subprocess.CompletedProcess([], 1, b"", b"err")
        sacct_fail = subprocess.CompletedProcess([], 1, b"", b"err")
        with patch("subprocess.run", side_effect=[scontrol_fail, sacct_fail]):
            state, exit_code = runner._query_state("42")
        assert state is None
        assert exit_code == ""

    def test_parse_scontrol_output(self) -> None:
        """The parser handles real scontrol output (multi-line key=value)."""
        sample = (
            "JobId=10 JobName=wrap\n"
            "   UserId=vagrant(1000) GroupId=vagrant(1000)\n"
            "   JobState=COMPLETED Reason=None Dependency=(null)\n"
            "   ExitCode=0:0 Restarts=0\n"
            "   Partition=debug\n"
        )
        state, exit_code = SlurmRunner._parse_scontrol_show_job(sample)
        assert state == "COMPLETED"
        assert exit_code == "0:0"

    def test_parse_scontrol_output_with_invalid_jobid(self) -> None:
        """When scontrol fails to find the job, JobState is absent."""
        sample = "slurm_load_jobs error: Invalid job id specified"
        state, exit_code = SlurmRunner._parse_scontrol_show_job(sample)
        assert state is None
        assert exit_code == ""


class TestSlurmRunnerSitecustomizeInjection:
    """v0.6.11: SlurmRunner must materialize sitecustomize.py to the
    capsule_dir (shared FS) and add it to PYTHONPATH in the wrap
    script. Without this, wire-level capture silently no-ops on the
    compute node — proven by the live e2e test on a real SLURM cluster
    that produced an empty model-calls.jsonl in v0.6.10."""

    def test_sitecustomize_written_to_capsule_dir(self, tmp_path: Path) -> None:
        sbatch = subprocess.CompletedProcess([], 0, b"1\n", b"")
        scontrol = subprocess.CompletedProcess(
            [], 0, b"JobId=1 JobState=COMPLETED ExitCode=0:0", b"",
        )
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir(parents=True, exist_ok=True)
        spec = RunnerJobSpec(
            run_id="01TESTSITECUSTOMIZE0000000",
            command=["echo", "hi"], capsule_dir=capsule_dir,
            env={}, runner_options={"partition": "debug"},
        )
        with patch("subprocess.run", side_effect=[sbatch, scontrol]):
            SlurmRunner().run(spec)
        sitecustomize = capsule_dir / "sitecustomize.py"
        assert sitecustomize.exists(), (
            "SlurmRunner did not materialize sitecustomize.py in capsule_dir"
        )
        # The file content must include the hook-install incantation
        # so the compute-node Python actually loads novafabric hooks.
        text = sitecustomize.read_text()
        assert "novafabric.capture.hooks" in text
        assert "install_all" in text

    def test_pythonpath_in_wrap_script_includes_capsule_dir(
        self, tmp_path: Path,
    ) -> None:
        """The wrap script's PYTHONPATH must point at capsule_dir so
        Python finds sitecustomize.py at startup."""
        sbatch = subprocess.CompletedProcess([], 0, b"1\n", b"")
        scontrol = subprocess.CompletedProcess(
            [], 0, b"JobId=1 JobState=COMPLETED ExitCode=0:0", b"",
        )
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir(parents=True, exist_ok=True)
        spec = RunnerJobSpec(
            run_id="01X", command=["echo"], capsule_dir=capsule_dir,
            env={}, runner_options={"partition": "debug"},
        )
        captured: list[list[str]] = []

        def _record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            captured.append(argv)
            if argv and argv[0].endswith("sbatch"):
                return sbatch
            return scontrol

        with patch("subprocess.run", side_effect=_record):
            SlurmRunner().run(spec)
        sbatch_argv = captured[0]
        # The wrap script is the last argv entry after --wrap.
        wrap_idx = sbatch_argv.index("--wrap")
        wrap_script = sbatch_argv[wrap_idx + 1]
        assert "export PYTHONPATH=" in wrap_script
        assert str(capsule_dir) in wrap_script

    def test_existing_pythonpath_preserved(self, tmp_path: Path) -> None:
        """If the orchestrator passes PYTHONPATH in env (e.g. user's
        venv site-packages), it must be appended after the capsule_dir
        prefix — not overwritten."""
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir(parents=True, exist_ok=True)
        # We test the helper directly here (not the full SlurmRunner.run)
        # to isolate the PYTHONPATH-merge logic. The full-flow argv
        # tests above already exercise the runner end-to-end.
        from novafabric.runners._slurm import _build_wrap_script
        env = {"NOVAFABRIC_SPAN_ID": "0" * 16, "PYTHONPATH": "/user/site"}
        script = _build_wrap_script(["echo", "hi"], env, capsule_dir)
        assert "export PYTHONPATH=" in script
        assert str(capsule_dir) in script
        # The user's path is preserved appended after capsule_dir:
        assert "/user/site" in script


class TestSlurmRunnerArgvShape:
    """Capture the sbatch argv to verify flag construction."""

    def _capture_argv(self, spec: RunnerJobSpec) -> list[str]:
        sbatch = subprocess.CompletedProcess([], 0, b"1;cluster\n", b"")
        scontrol = subprocess.CompletedProcess(
            [], 0, b"JobId=1 JobState=COMPLETED ExitCode=0:0", b"",
        )
        sacct = subprocess.CompletedProcess([], 0, b"1|COMPLETED|0:0\n", b"")
        captured: list[list[str]] = []

        def _record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            captured.append(argv)
            if argv and argv[0].endswith("sbatch"):
                return sbatch
            if argv and argv[0].endswith("scontrol"):
                return scontrol
            return sacct

        with patch("subprocess.run", side_effect=_record):
            SlurmRunner().run(spec)
        return captured[0]

    def test_partition_flag_present(self, tmp_path: Path) -> None:
        argv = self._capture_argv(_spec(
            tmp_path, ["echo"], runner_options={"partition": "gpu-a100"},
        ))
        assert any(a == "--partition=gpu-a100" for a in argv)

    def test_optional_flags_passed(self, tmp_path: Path) -> None:
        argv = self._capture_argv(_spec(
            tmp_path, ["echo"], runner_options={
                "partition": "gpu", "account": "novafabric",
                "qos": "premium", "nodes": 2, "gres": "gpu:a100:1",
                "mem": "32G", "constraint": "a100",
            },
        ))
        for expected in (
            "--account=novafabric", "--qos=premium", "--nodes=2",
            "--gres=gpu:a100:1", "--mem=32G", "--constraint=a100",
        ):
            assert expected in argv, (
                f"expected {expected!r} in sbatch argv, got: {argv}"
            )

    def test_time_derived_from_timeout_when_explicit_unset(
        self, tmp_path: Path
    ) -> None:
        argv = self._capture_argv(_spec(
            tmp_path, ["echo"], runner_options={"partition": "gpu"},
            timeout_s=600.0,
        ))
        # 600s = 10 min
        assert "--time=10" in argv

    def test_explicit_time_takes_precedence(self, tmp_path: Path) -> None:
        argv = self._capture_argv(_spec(
            tmp_path, ["echo"], runner_options={
                "partition": "gpu", "time": "12:00:00",
            },
            timeout_s=600.0,
        ))
        assert "--time=12:00:00" in argv


class TestShellQuoting:
    def test_simple_arg_unquoted(self) -> None:
        assert _shell_quote("hello") == "hello"

    def test_arg_with_space_quoted(self) -> None:
        assert _shell_quote("hello world") == "'hello world'"

    def test_arg_with_quote_escaped(self) -> None:
        # POSIX: a' becomes 'a'\''
        assert _shell_quote("don't") == "'don'\"'\"'t'"


class TestWrapScript:
    def test_wrap_exports_novafabric_env_and_execs(self, tmp_path: Path) -> None:
        script = _build_wrap_script(
            command=["python", "agent.py"],
            env={"NOVAFABRIC_SPAN_ID": "abc", "PATH": "/usr/bin", "OTHER": "ignored"},
            capsule_dir=tmp_path / "cap",
        )
        assert "set -e" in script
        assert "export NOVAFABRIC_SPAN_ID=abc" in script
        assert "export PATH=/usr/bin" in script
        # OTHER is filtered (not NOVAFABRIC_*, not PATH).
        assert "OTHER" not in script
        # Capsule dir is set explicitly so the in-job NOVAFABRIC_CAPSULE_DIR
        # reflects the shared-FS path.
        assert f"export NOVAFABRIC_CAPSULE_DIR={tmp_path / 'cap'}" in script
        assert "exec python agent.py" in script


@pytest.mark.skipif(
    os.environ.get("NOVAFABRIC_TEST_LIVE_SLURM", "") != "1",
    reason="opt-in: set NOVAFABRIC_TEST_LIVE_SLURM=1 to run against a real SLURM cluster",
)
class TestSlurmRunnerLiveSmoke:
    """Opt-in. Requires a reachable SLURM submission node and a partition
    the executing user can submit to.

    ``capsule_dir`` must be on a filesystem shared between the submit node and
    any compute node SLURM may allocate (NFS, Lustre, GPFS …).  Local ``/tmp``
    is node-local and will cause jobs to fail with a non-zero exit when the
    scheduler places them on a different node.

    Set ``NOVAFABRIC_SLURM_SHARED_DIR`` to a writable shared-FS path before
    running these tests (e.g. ``/home/vagrant`` or ``/scratch``).  The fixture
    creates a unique sub-directory there and removes it on teardown.
    """

    @pytest.fixture()
    def shared_tmp(self, tmp_path: Path) -> Path:  # type: ignore[override]
        import shutil
        import uuid

        base_env = os.environ.get("NOVAFABRIC_SLURM_SHARED_DIR", "")
        if not base_env:
            yield tmp_path
            return
        base = Path(base_env)
        d = base / f"nova-live-{uuid.uuid4().hex[:8]}"
        d.mkdir(parents=True, exist_ok=True)
        try:
            yield d
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_echo_succeeds(self, shared_tmp: Path) -> None:
        result = SlurmRunner().run(_spec(
            shared_tmp, ["echo", "hello-from-slurm"],
            runner_options={"partition": "debug", "time": "00:01:00"},
            timeout_s=300.0,
        ))
        assert result.exit_code == 0, (
            f"runner_status={result.runner_status} "
            f"final_state={result.runner_metadata.get('final_state')} "
            f"stderr={result.stderr.decode(errors='replace')[:500]}"
        )

    @pytest.mark.skipif(
        not os.environ.get("NOVAFABRIC_SLURM_VENV_PYTHON"),
        reason=(
            "set NOVAFABRIC_SLURM_VENV_PYTHON to the venv Python path visible "
            "from compute nodes (e.g. /home/vagrant/novafabric-venv/bin/python3) "
            "to enable the wire-capture end-to-end test"
        ),
    )
    def test_sitecustomize_fires_on_compute(self, shared_tmp: Path) -> None:
        """SlurmRunner injects sitecustomize.py so wire-level httpx capture
        fires on compute nodes.  A failed network request (ConnectError,
        Timeout) is still recorded — the hook uses try/finally."""
        import uuid

        from novafabric.runners._types import RunnerJobSpec as _Spec

        capsule_dir = shared_tmp / f"wire-{uuid.uuid4().hex[:6]}"
        capsule_dir.mkdir(parents=True, exist_ok=True)

        venv_python = os.environ["NOVAFABRIC_SLURM_VENV_PYTHON"]
        venv_site = os.environ.get("NOVAFABRIC_SLURM_VENV_SITE", "")

        py_cmd = (
            "import httpx\n"
            "try:\n"
            "    httpx.get('https://api.openai.com/v1/models', timeout=5)\n"
            "except Exception:\n"
            "    pass\n"
        )

        spec = _Spec(
            run_id="01TESTWIRE000000000000000000",
            command=[venv_python, "-c", py_cmd],
            capsule_dir=capsule_dir,
            env={
                "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
                "NOVAFABRIC_SPAN_ID": "0" * 16,
                **({"PYTHONPATH": venv_site} if venv_site else {}),
            },
            runner_options={"partition": "debug", "time": "00:01:00"},
            timeout_s=120.0,
        )
        SlurmRunner().run(spec)
        jsonl = capsule_dir / "model-calls.jsonl"
        assert jsonl.exists(), (
            "model-calls.jsonl not found — sitecustomize injection or "
            "wire capture did not fire on the compute node"
        )
