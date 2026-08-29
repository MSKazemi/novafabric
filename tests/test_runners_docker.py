"""Unit tests for the DockerRunner (ADR-0025 Runners.2).

These tests mock subprocess.run so they pass on any machine — no docker
daemon required. End-to-end testing against a real docker daemon is
out of scope here (would require an image and a running daemon in CI).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.runners import ContainerEvalError, DockerRunner, RunnerJobSpec, RunnerSpec
from novafabric.runners._docker import _coerce_str_dict, _coerce_str_list


def _spec(
    tmp_path: Path, command: list[str], **kwargs: object
) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTDOCKER0000000000000000",
        command=command,
        capsule_dir=capsule_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
            "PATH": "/usr/bin",
        },
        **kwargs,  # type: ignore[arg-type]
    )


class TestDockerRunnerProtocol:
    def test_satisfies_runner_spec(self) -> None:
        runner: RunnerSpec = DockerRunner()
        assert runner.name == "docker"
        assert "docker" in runner.description.lower()


class TestDockerRunnerSupports:
    def test_returns_false_when_binary_not_on_path(self) -> None:
        with patch("shutil.which", return_value=None):
            ok, why = DockerRunner().supports()
        assert ok is False
        assert "not found" in why

    def test_returns_false_when_daemon_not_reachable(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["docker", "info"], returncode=1,
            stdout=b"", stderr=b"Cannot connect to the Docker daemon\n",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("subprocess.run", return_value=completed):
            ok, why = DockerRunner().supports()
        assert ok is False
        assert "not reachable" in why or "Cannot connect" in why

    def test_returns_true_when_daemon_responds(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["docker", "info"], returncode=0,
            stdout=b"24.0.7\n", stderr=b"",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("subprocess.run", return_value=completed):
            ok, why = DockerRunner().supports()
        assert ok is True
        assert why == ""


class TestDockerRunnerArgvConstruction:
    """The runner builds a `docker run` argv from the spec. We assert
    the argv has the expected shape — image, env, volumes, command —
    by capturing what subprocess.run is called with."""

    def _run_with_recorded_argv(
        self, spec: RunnerJobSpec
    ) -> list[str]:
        """Return the argv that DockerRunner would have invoked."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b"",
        )
        captured: list[list[str]] = []

        def _record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            captured.append(argv)
            return completed

        with patch("subprocess.run", side_effect=_record):
            DockerRunner().run(spec)
        assert len(captured) == 1, f"expected 1 docker invocation, got {len(captured)}"
        return captured[0]

    def test_includes_run_rm_image_and_command(self, tmp_path: Path) -> None:
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "myorg/agent:abc"},
        ))
        assert argv[0] == "docker"
        assert argv[1:3] == ["run", "--rm"]
        # Image appears before the command, command at the end.
        image_idx = argv.index("myorg/agent:abc")
        assert argv[image_idx + 1:] == ["python", "agent.py"]

    def test_passes_novafabric_env_into_container(self, tmp_path: Path) -> None:
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "x"},
        ))
        # NOVAFABRIC_CAPSULE_DIR is rewritten to the in-container path,
        # not the host path. NOVAFABRIC_SPAN_ID is passed through.
        assert any(
            a.startswith("NOVAFABRIC_CAPSULE_DIR=/novafabric/capsule")
            for a in argv
        ), f"expected -e NOVAFABRIC_CAPSULE_DIR=/novafabric/capsule, got {argv}"
        assert any(
            a == "NOVAFABRIC_SPAN_ID=" + "0" * 16
            for a in argv
        )

    def test_does_not_leak_arbitrary_host_env(self, tmp_path: Path) -> None:
        """PATH and other host env vars must NOT be auto-forwarded to the
        container — only NOVAFABRIC_* and explicit extra_env."""
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "x"},
        ))
        assert not any(a.startswith("PATH=") for a in argv), (
            f"PATH leaked into docker env: {[a for a in argv if a.startswith('PATH=')]}"
        )

    def test_extra_env_is_passed(self, tmp_path: Path) -> None:
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "x", "extra_env": {"DEBUG": "1"}},
        ))
        assert any(a == "DEBUG=1" for a in argv)

    def test_capsule_volume_mounted(self, tmp_path: Path) -> None:
        spec = _spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "x"},
        )
        argv = self._run_with_recorded_argv(spec)
        expected_mount = f"{spec.capsule_dir}:/novafabric/capsule"
        assert "-v" in argv
        assert expected_mount in argv

    def test_extra_volumes_are_mounted(self, tmp_path: Path) -> None:
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={
                "image": "x",
                "extra_volumes": ["/host/a:/c/a:ro", "/host/b:/c/b"],
            },
        ))
        # Both extra volumes should appear after a -v flag.
        assert "/host/a:/c/a:ro" in argv
        assert "/host/b:/c/b" in argv

    def test_network_option_passed(self, tmp_path: Path) -> None:
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "x", "network": "host"},
        ))
        idx = argv.index("--network")
        assert argv[idx + 1] == "host"

    def test_user_option_passed(self, tmp_path: Path) -> None:
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "x", "user": "1000:1000"},
        ))
        idx = argv.index("--user")
        assert argv[idx + 1] == "1000:1000"

    def test_workdir_option_passed(self, tmp_path: Path) -> None:
        argv = self._run_with_recorded_argv(_spec(
            tmp_path, ["python", "agent.py"],
            runner_options={"image": "x", "workdir": "/work"},
        ))
        idx = argv.index("--workdir")
        assert argv[idx + 1] == "/work"


class TestDockerRunnerErrorPaths:
    def test_missing_image_returns_failed_setup(self, tmp_path: Path) -> None:
        result = DockerRunner().run(_spec(tmp_path, ["python"]))
        assert result.exit_code == 127
        assert result.runner_status == "failed_setup"
        assert "image" in (result.runner_error or "").lower()

    def test_docker_exit_125_marked_as_failed_setup(self, tmp_path: Path) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=125,
            stdout=b"", stderr=b"docker: Error response from daemon: pull access denied\n",
        )
        with patch("subprocess.run", return_value=completed):
            result = DockerRunner().run(_spec(
                tmp_path, ["python", "x.py"],
                runner_options={"image": "private/image:tag"},
            ))
        assert result.exit_code == 125
        assert result.runner_status == "failed_setup"

    def test_workload_nonzero_exit_marked_completed(self, tmp_path: Path) -> None:
        """If the container ran the workload and the workload failed
        (non-125 exit), runner_status is still 'completed' — the
        runner did its job, the workload didn't."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=42,
            stdout=b"out", stderr=b"err",
        )
        with patch("subprocess.run", return_value=completed):
            result = DockerRunner().run(_spec(
                tmp_path, ["x"], runner_options={"image": "i"},
            ))
        assert result.exit_code == 42
        assert result.runner_status == "completed"
        assert result.stdout == b"out"
        assert result.stderr == b"err"

    def test_timeout_returns_124_and_timeout_status(self, tmp_path: Path) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker"], timeout=0.5),
        ):
            result = DockerRunner().run(_spec(
                tmp_path, ["x"], runner_options={"image": "i"},
                timeout_s=0.5,
            ))
        assert result.exit_code == 124
        assert result.runner_status == "timeout"
        assert result.runner_error is not None

    def test_metadata_includes_image(self, tmp_path: Path) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b"",
        )
        with patch("subprocess.run", return_value=completed):
            result = DockerRunner().run(_spec(
                tmp_path, ["x"], runner_options={"image": "myorg/agent:tag"},
            ))
        assert result.runner_metadata.get("image") == "myorg/agent:tag"


class TestCoercionHelpers:
    """Runner options arrive in two shapes and both must survive.

    A YAML spec supplies real lists and dicts. The CLI's ``--runner-option
    k=v`` supplies **strings**, because that is all a shell argument can be.
    Discarding the string form silently dropped every option a CLI user
    passed: `--runner-option volumes=/host:/work` mounted nothing, and the
    container then failed with `can't open file '/work/payload.py'` — a
    misconfiguration reported as a workload error.
    """

    def test_coerce_str_dict_filters_junk(self) -> None:
        assert _coerce_str_dict("not a dict") == {}, "no k=v pair, nothing to take"
        assert _coerce_str_dict({"a": 1}) == {"a": "1"}
        assert _coerce_str_dict(7) == {}
        assert _coerce_str_dict(None) == {}

    def test_coerce_str_dict_parses_the_cli_string_form(self) -> None:
        assert _coerce_str_dict("a=1,b=2") == {"a": "1", "b": "2"}
        assert _coerce_str_dict(" a = 1 ") == {"a": "1"}, "shell quoting leaves spaces"

    def test_coerce_str_list_filters_junk(self) -> None:
        assert _coerce_str_list(["a", 1]) == ["a", "1"]
        assert _coerce_str_list(7) == []
        assert _coerce_str_list(None) == []

    def test_coerce_str_list_parses_the_cli_string_form(self) -> None:
        """The regression that a real Docker run exposed."""
        assert _coerce_str_list("/host:/work") == ["/host:/work"]
        assert _coerce_str_list("a,b") == ["a", "b"]
        assert _coerce_str_list("a, ,b") == ["a", "b"], "empty items dropped"
        assert _coerce_str_list("") == []


class TestNonRootSafetyDefaults:
    """Anti-patterns from ADR-0025 §Anti-patterns: docker runner must
    NOT auto-add --privileged, --pid host, etc. We assert the argv
    does NOT contain these even when the user provides no options
    that explicitly forbid them."""

    def test_no_privileged_flag_by_default(self, tmp_path: Path) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        captured: list[list[str]] = []
        with patch("subprocess.run", side_effect=lambda a, **k: (captured.append(a), completed)[1]):
            DockerRunner().run(_spec(
                tmp_path, ["x"], runner_options={"image": "i"},
            ))
        argv = captured[0]
        assert "--privileged" not in argv
        assert "--pid" not in argv
        assert "--ipc" not in argv
        assert "host" not in argv  # no implicit --network host

    def test_docker_socket_not_mounted(self, tmp_path: Path) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        captured: list[list[str]] = []
        with patch("subprocess.run", side_effect=lambda a, **k: (captured.append(a), completed)[1]):
            DockerRunner().run(_spec(
                tmp_path, ["x"], runner_options={"image": "i"},
            ))
        argv = captured[0]
        assert not any("docker.sock" in a for a in argv), (
            f"docker socket leaked into argv: {argv}"
        )


_VALID_DIGEST = "sha256:" + "a" * 64


def _docker_available() -> bool:
    with patch("shutil.which", return_value="/usr/bin/docker"):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"24.0.7\n", stderr=b""
        )
        with patch("subprocess.run", return_value=completed):
            ok, _ = DockerRunner().supports()
    return ok


class TestRunEvalContainer:
    """Unit tests for DockerRunner.run_eval_container() (ADR-0033 E-4)."""

    def _docker_ok(self) -> tuple:
        return (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=b"24.0.7\n", stderr=b""
                ),
            ),
        )

    def _run(
        self,
        capsule_path: Path,
        *,
        digest: str = _VALID_DIGEST,
        image_ref: str = "ghcr.io/org/eval:v1",
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
        subprocess_result: subprocess.CompletedProcess | None = None,
    ) -> tuple[list[list[str]], tuple[int, bytes, bytes]]:
        """Helper: run run_eval_container with docker mocked as available,
        capture all subprocess.run call args, return them + the result."""
        if command is None:
            command = ["python", "-m", "suite", "--capsule", "/novafabric/eval-capsule"]
        if subprocess_result is None:
            subprocess_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b'{"ok": true}', stderr=b""
            )

        calls: list[list[str]] = []
        call_count = 0

        def _side_effect(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            nonlocal call_count
            call_count += 1
            calls.append(argv)
            # First call is supports() → docker info; subsequent call is docker run.
            if call_count == 1:
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=b"24.0.7\n", stderr=b""
                )
            return subprocess_result

        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("subprocess.run", side_effect=_side_effect):
            result = DockerRunner().run_eval_container(
                image_ref=image_ref,
                digest=digest,
                capsule_path=capsule_path,
                command=command,
                env=env,
                timeout_s=timeout_s,
            )
        return calls, result

    def test_bad_digest_format_raises_container_eval_error(self, tmp_path: Path) -> None:
        dr = DockerRunner()
        with pytest.raises(ContainerEvalError, match="sha256"):
            dr.run_eval_container(
                image_ref="ghcr.io/org/eval:v1",
                digest="notadigest",
                capsule_path=tmp_path,
                command=["python", "-m", "suite"],
            )

    def test_digest_without_sha256_prefix_raises(self, tmp_path: Path) -> None:
        dr = DockerRunner()
        with pytest.raises(ContainerEvalError):
            dr.run_eval_container(
                image_ref="ghcr.io/org/eval:v1",
                digest="abc123def456",
                capsule_path=tmp_path,
                command=["run"],
            )

    def test_docker_unavailable_raises_container_eval_error(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value=None):
            dr = DockerRunner()
            with pytest.raises(ContainerEvalError, match="not available"):
                dr.run_eval_container(
                    image_ref="ghcr.io/org/eval:v1",
                    digest=_VALID_DIGEST,
                    capsule_path=tmp_path,
                    command=["run"],
                )

    def test_uses_pinned_image_ref_with_digest(self, tmp_path: Path) -> None:
        calls, _ = self._run(
            tmp_path,
            image_ref="ghcr.io/org/eval:v1",
            digest=_VALID_DIGEST,
        )
        # calls[0] = docker info (supports), calls[1] = docker run
        run_argv = calls[1]
        expected_image = f"ghcr.io/org/eval:v1@{_VALID_DIGEST}"
        assert expected_image in run_argv, (
            f"expected pinned ref {expected_image!r} in argv: {run_argv}"
        )

    def test_capsule_mounted_readonly_at_eval_path(self, tmp_path: Path) -> None:
        capsule_dir = tmp_path / "mycapsule"
        capsule_dir.mkdir()
        calls, _ = self._run(capsule_dir)
        run_argv = calls[1]
        expected_mount = f"{capsule_dir}:/novafabric/eval-capsule:ro"
        assert "-v" in run_argv
        assert expected_mount in run_argv, (
            f"expected ro mount {expected_mount!r} in argv: {run_argv}"
        )

    def test_extra_env_passed_as_e_flags(self, tmp_path: Path) -> None:
        calls, _ = self._run(tmp_path, env={"SUITE_LEVEL": "2", "DEBUG": "1"})
        run_argv = calls[1]
        assert "SUITE_LEVEL=2" in run_argv
        assert "DEBUG=1" in run_argv

    def test_command_appended_after_image(self, tmp_path: Path) -> None:
        cmd = ["python", "-m", "gaia_eval", "--level", "1"]
        calls, _ = self._run(tmp_path, command=cmd)
        run_argv = calls[1]
        image_ref = f"ghcr.io/org/eval:v1@{_VALID_DIGEST}"
        img_idx = run_argv.index(image_ref)
        assert run_argv[img_idx + 1:] == cmd

    def test_returns_exit_code_stdout_stderr(self, tmp_path: Path) -> None:
        proc = subprocess.CompletedProcess(
            args=[], returncode=42, stdout=b"out-data", stderr=b"err-data"
        )
        _, result = self._run(tmp_path, subprocess_result=proc)
        assert result == (42, b"out-data", b"err-data")

    def test_docker_exit_125_raises_container_eval_error(self, tmp_path: Path) -> None:
        proc = subprocess.CompletedProcess(
            args=[], returncode=125,
            stdout=b"", stderr=b"docker: manifest digest mismatch\n",
        )
        with pytest.raises(ContainerEvalError, match="setup failure"):
            self._run(tmp_path, subprocess_result=proc)

    def test_timeout_raises_container_eval_error(self, tmp_path: Path) -> None:
        call_count = 0

        def _side(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=b"24.0.7\n", stderr=b""
                )
            raise subprocess.TimeoutExpired(cmd=["docker"], timeout=0.1)

        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("subprocess.run", side_effect=_side):
            with pytest.raises(ContainerEvalError, match="deadline"):
                DockerRunner().run_eval_container(
                    image_ref="ghcr.io/org/eval:v1",
                    digest=_VALID_DIGEST,
                    capsule_path=tmp_path,
                    command=["run"],
                    timeout_s=0.1,
                )

    def test_no_privileged_or_host_net_in_eval_argv(self, tmp_path: Path) -> None:
        calls, _ = self._run(tmp_path)
        run_argv = calls[1]
        assert "--privileged" not in run_argv
        assert "--pid" not in run_argv
        assert "--network" not in run_argv

    def test_run_uses_rm_flag(self, tmp_path: Path) -> None:
        calls, _ = self._run(tmp_path)
        run_argv = calls[1]
        assert "--rm" in run_argv


@pytest.mark.skipif(
    DockerRunner().supports()[0] is False,
    reason="docker daemon not available; skipping live smoke",
)
class TestDockerRunnerLiveSmoke:
    """Skipped unless a docker daemon is reachable. Catches integration
    issues that mocked tests miss (image pull, volume mounting actually
    works on this OS, etc.)."""

    def test_alpine_echo_succeeds(self, tmp_path: Path) -> None:
        result = DockerRunner().run(_spec(
            tmp_path, ["echo", "hello"],
            runner_options={"image": "alpine:3.20"},
            timeout_s=60.0,
        ))
        assert result.exit_code == 0, (
            f"runner_status={result.runner_status} "
            f"stderr={result.stderr.decode(errors='replace')}"
        )
        assert b"hello" in result.stdout
