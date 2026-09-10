"""Every runner that leaves the host forwards an allowlist, not the environment.

This is the cross-runner guard for defect **B2** (ADR-0270). The per-runner
suites already check their own behaviour; what they could not catch is the
thing that actually happened — Docker and SLURM each filtered, Kubernetes did
not, and nothing compared them. A property delegated to N call sites holds at
N-1 of them, so it is asserted here **once, over all of them**.

``LocalRunner`` is deliberately exempt and asserted to be exempt: it runs the
user's own workload as the user on the user's machine, so its environment
never crosses a trust boundary and stripping it would break capture outright.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.runners import DockerRunner, KubernetesRunner, RunnerJobSpec, SlurmRunner
from novafabric.runners._env import forwardable_env

#: A secret that must never appear in anything a runner sends off-host.
SECRET_KEY = "OPENAI_API_KEY"
SECRET_VALUE = "sk-live-must-never-be-transported"


def _spec(tmp_path: Path, **kwargs: object) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTENVFWD0000000000000",
        command=["python", "agent.py"],
        capsule_dir=capsule_dir,
        # Exactly what capture/orchestrator.py builds: dict(os.environ) plus
        # NovaFabric's own vars. The secret is what a real submitting shell has.
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/someone",
            "AWS_SECRET_ACCESS_KEY": "aws-must-never-be-transported",
            SECRET_KEY: SECRET_VALUE,
        },
        **kwargs,  # type: ignore[arg-type]
    )


class TestForwardableEnvHelper:
    def test_keeps_novafabric_vars(self) -> None:
        out = forwardable_env({"NOVAFABRIC_SPAN_ID": "a", SECRET_KEY: SECRET_VALUE})
        assert out == {"NOVAFABRIC_SPAN_ID": "a"}

    def test_drops_everything_else_by_default(self) -> None:
        out = forwardable_env({"PATH": "/usr/bin", "HOME": "/h", SECRET_KEY: "x"})
        assert out == {}

    def test_also_allow_is_the_only_way_in(self) -> None:
        env = {"PATH": "/usr/bin", SECRET_KEY: "x"}
        assert forwardable_env(env, also_allow={"PATH"}) == {"PATH": "/usr/bin"}

    def test_does_not_mutate_the_input(self) -> None:
        env = {"NOVAFABRIC_A": "1", SECRET_KEY: SECRET_VALUE}
        forwardable_env(env)
        assert SECRET_KEY in env, "helper mutated the caller's environment"

    def test_prefix_match_is_not_a_substring_match(self) -> None:
        # MY_NOVAFABRIC_TOKEN contains the prefix but does not start with it.
        assert forwardable_env({"MY_NOVAFABRIC_TOKEN": "x"}) == {}


def _kubernetes_manifest_env(tmp_path: Path, **runner_options: object) -> list[dict]:
    """Run KubernetesRunner far enough to capture the manifest it applies."""
    apply_fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
    with patch("subprocess.run", return_value=apply_fail) as run:
        KubernetesRunner().run(_spec(
            tmp_path,
            runner_options={"image": "img", "namespace": "ns", **runner_options},
        ))
    applied = json.loads(run.call_args.kwargs["input"].decode())
    return applied["spec"]["template"]["spec"]["containers"][0]["env"]


def _docker_argv(tmp_path: Path, **runner_options: object) -> list[str]:
    fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=fail) as run:
        DockerRunner().run(_spec(
            tmp_path, runner_options={"image": "img", **runner_options},
        ))
    return [str(a) for a in run.call_args.args[0]]


def _slurm_script(tmp_path: Path) -> str:
    fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
    with patch("shutil.which", return_value="/usr/bin/sbatch"), \
         patch("subprocess.run", return_value=fail) as run:
        SlurmRunner().run(_spec(
            tmp_path, runner_options={"partition": "normal"},
        ))
    return " ".join(str(a) for a in run.call_args.args[0])


class TestNoRunnerTransportsTheSubmittingEnvironment:
    """The one assertion B2 needed and nothing made."""

    def test_kubernetes_job_object_carries_no_secret(self, tmp_path: Path) -> None:
        envs = _kubernetes_manifest_env(tmp_path)
        names = {e["name"] for e in envs}
        assert SECRET_KEY not in names, (
            f"B2 regression: the submitting shell's {SECRET_KEY} reached the Job "
            f"object, which is readable with `get job` and persisted in etcd. "
            f"env={sorted(names)}"
        )
        assert not any(SECRET_VALUE in e.get("value", "") for e in envs)
        # Non-secret host vars are equally not ours to forward.
        assert "HOME" not in names and "PATH" not in names
        # ...but NovaFabric's own vars must survive, or capture never fires.
        assert "NOVAFABRIC_SPAN_ID" in names
        assert any(
            e["name"] == "NOVAFABRIC_CAPSULE_DIR"
            and e["value"] == "/novafabric/capsule"
            for e in envs
        )

    def test_kubernetes_extra_env_is_the_explicit_opt_in(self, tmp_path: Path) -> None:
        envs = _kubernetes_manifest_env(tmp_path, extra_env={"DEBUG": "1"})
        assert {"name": "DEBUG", "value": "1"} in envs
        assert SECRET_KEY not in {e["name"] for e in envs}

    def test_docker_argv_carries_no_secret(self, tmp_path: Path) -> None:
        argv = _docker_argv(tmp_path)
        assert not any(a.startswith(f"{SECRET_KEY}=") for a in argv), (
            f"secret reached `docker run -e`, visible in `docker inspect`: {argv}"
        )
        assert not any(SECRET_VALUE in a for a in argv)

    def test_slurm_script_carries_no_secret(self, tmp_path: Path) -> None:
        script = _slurm_script(tmp_path)
        assert SECRET_VALUE not in script, "secret exported into the sbatch --wrap script"
        assert f"export {SECRET_KEY}=" not in script
        # PATH is SLURM's one documented exception — the compute node needs it.
        assert "export PATH=" in script

    @pytest.mark.parametrize("secret", [SECRET_KEY, "AWS_SECRET_ACCESS_KEY"])
    def test_no_off_host_runner_transports_any_secret(
        self, tmp_path: Path, secret: str
    ) -> None:
        """One assertion spanning every off-host runner, so a new runner that
        forgets to filter fails here rather than shipping."""
        k8s = json.dumps(_kubernetes_manifest_env(tmp_path))
        docker = " ".join(_docker_argv(tmp_path))
        slurm = _slurm_script(tmp_path)
        for label, payload in (
            ("kubernetes", k8s), ("docker", docker), ("slurm", slurm),
        ):
            assert secret not in payload, f"{label} runner transported {secret}"


class TestLocalRunnerIsDeliberatelyExempt:
    def test_local_runner_does_not_import_the_filter(self) -> None:
        """LocalRunner must keep the full environment: the workload is the
        user's own agent on the user's own machine and needs its credentials.
        Asserting the exemption keeps it a decision rather than an oversight."""
        source = Path("src/novafabric/runners/_local.py").read_text(encoding="utf-8")
        assert "forwardable_env" not in source, (
            "LocalRunner started filtering its environment — that breaks capture "
            "for every local agent that needs a provider key. See ADR-0270."
        )
