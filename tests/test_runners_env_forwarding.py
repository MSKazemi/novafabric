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
from typing import Any
from unittest.mock import patch

import pytest

from novafabric.runners import DockerRunner, KubernetesRunner, RunnerJobSpec, SlurmRunner
from novafabric.runners._env import forwardable_env
from novafabric.runners._lsf import LSFRunner
from novafabric.runners._pbs import PBSRunner

#: A secret that must never appear in anything a runner sends off-host.
SECRET_KEY = "OPENAI_API_KEY"
SECRET_VALUE = "sk-live-must-never-be-transported"
NF_SECRET_KEY = "NOVAFABRIC_SERVER_TOKEN"


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
            # Shares the NOVAFABRIC_ prefix capture needs, but is a server credential.
            NF_SECRET_KEY: "nf-token-must-never-be-transported",
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

    @pytest.mark.parametrize("name", [
        "NOVAFABRIC_SERVER_TOKEN", "NOVAFABRIC_TOKEN", "NOVAFABRIC_API_KEY",
        "NOVAFABRIC_SCIM_TOKEN", "NOVAFABRIC_POSTGRES_DSN", "NOVAFABRIC_METADATA_DSN",
        "NOVAFABRIC_DB_URL", "NOVAFABRIC_SERVE_TOKEN", "NOVAFABRIC_SIGNING_KEY",
    ])
    def test_a_novafabric_credential_is_not_forwarded(self, name: str) -> None:
        assert name not in forwardable_env({name: "secret"})
        assert forwardable_env({name: "s"}, also_allow={name}) == {name: "s"}, (
            "an explicit extra_env must still be able to send it"
        )

    @pytest.mark.parametrize("name", [
        "NOVAFABRIC_CAPSULE_DIR", "NOVAFABRIC_SPAN_ID", "NOVAFABRIC_HOME",
        "NOVAFABRIC_SERVER_URL", "NOVAFABRIC_OFFLINE_KEY_PATH", "NOVAFABRIC_SESSION_ID",
    ])
    def test_what_capture_reads_in_the_job_is_still_forwarded(self, name: str) -> None:
        assert forwardable_env({name: "v"}) == {name: "v"}

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
    """Everything ``sbatch`` is handed: its argv and the batch script on its stdin."""
    fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
    with patch("shutil.which", return_value="/usr/bin/sbatch"), \
         patch("subprocess.run", return_value=fail) as run:
        SlurmRunner().run(_spec(
            tmp_path, runner_options={"partition": "normal"},
        ))
    argv = " ".join(str(a) for a in run.call_args.args[0])
    return argv + "\n" + run.call_args.kwargs["input"].decode()


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
        assert SECRET_VALUE not in script, "secret exported into the sbatch batch script"
        assert f"export {SECRET_KEY}=" not in script
        # PATH is SLURM's one documented exception — the compute node needs it.
        assert "export PATH=" in script

    @pytest.mark.parametrize("secret", [SECRET_KEY, "AWS_SECRET_ACCESS_KEY", NF_SECRET_KEY])
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


class TestBatchSchedulersSubmitWithAMinimalEnvironment:
    """ADR-0311: filtering the job script was not enough.

    ``sbatch`` and ``bsub`` propagate the environment of the *submit process* into
    the job by default (SLURM ``--export=ALL``; LSF copies the submission
    environment). The runners used to call them with the user's whole shell, so the
    secret the script filter removed reached the job anyway. The submit process now
    gets only the scheduler client's own variables, login basics, ``NOVAFABRIC_*``
    and the operator's explicit ``extra_env``.
    """

    @staticmethod
    def _submit(runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                **options: object) -> tuple[dict[str, str], str]:
        monkeypatch.setenv(SECRET_KEY, SECRET_VALUE)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-must-never-be-transported")
        monkeypatch.setenv("SLURM_CONF", "/etc/slurm/slurm.conf")
        monkeypatch.setenv("LSF_ENVDIR", "/etc/lsf")
        fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
        with patch("shutil.which", return_value=f"/usr/bin/{runner}"), \
             patch("subprocess.run", return_value=fail) as run:
            if runner == "sbatch":
                SlurmRunner().run(_spec(tmp_path, runner_options={"partition": "n", **options}))
            else:
                LSFRunner().run(_spec(tmp_path, runner_options=dict(options)))
        call = run.call_args
        # Both submit commands read the job script from stdin (never argv, never disk).
        return call.kwargs["env"], call.kwargs["input"].decode()

    @pytest.mark.parametrize("runner", ["sbatch", "bsub"])
    def test_the_submit_process_does_not_hold_the_users_secrets(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env, _ = self._submit(runner, tmp_path, monkeypatch)
        assert SECRET_KEY not in env and "AWS_SECRET_ACCESS_KEY" not in env, (
            f"{runner} runs with the submitting shell's secrets, and propagates them "
            f"into the job by default: {sorted(env)}"
        )
        assert SECRET_VALUE not in json.dumps(env)
        client = "SLURM_CONF" if runner == "sbatch" else "LSF_ENVDIR"
        assert client in env, "the scheduler client's own configuration must survive"

    @pytest.mark.parametrize("runner", ["sbatch", "bsub"])
    def test_novafabric_credentials_and_lc_lookalikes_stay_behind(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(NF_SECRET_KEY, "nf-token-must-never-be-transported")
        monkeypatch.setenv("LC_SECRET", "not-a-locale")
        monkeypatch.setenv("LC_ALL", "C.UTF-8")
        env, script = self._submit(runner, tmp_path, monkeypatch)
        assert NF_SECRET_KEY not in env and "nf-token-must-never" not in script
        assert "LC_SECRET" not in env
        assert env["LC_ALL"] == "C.UTF-8"
        assert "NOVAFABRIC_SPAN_ID" in script, "capture's own variables must reach the job"

    @pytest.mark.parametrize("runner", ["slurm", "lsf"])
    def test_query_commands_run_with_the_client_environment(
        self, runner: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scontrol / sacct / bjobs / bkill used to run with the user's whole shell."""
        monkeypatch.setenv(SECRET_KEY, SECRET_VALUE)
        monkeypatch.setenv(NF_SECRET_KEY, "nf-token-must-never-be-transported")
        fail = subprocess.CompletedProcess([], 1, b"", b"")
        with patch("shutil.which", return_value="/usr/bin/x"), \
             patch("subprocess.run", return_value=fail) as run:
            if runner == "slurm":
                SlurmRunner()._query_state("42")
                SlurmRunner().supports()
            else:
                LSFRunner()._query_state("42")
                LSFRunner().supports()
                LSFRunner().cancel("42")
        assert run.call_count >= 3
        for call in run.call_args_list:
            env = call.kwargs.get("env")
            assert env is not None, f"{call.args[0][0]} inherits the whole shell"
            assert SECRET_KEY not in env and NF_SECRET_KEY not in env

    @pytest.mark.parametrize("runner", ["sbatch", "bsub"])
    def test_extra_env_is_the_explicit_opt_in(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env, script = self._submit(runner, tmp_path, monkeypatch, extra_env={"MODEL_TOKEN": "t1"})
        assert env["MODEL_TOKEN"] == "t1"
        assert "export MODEL_TOKEN=" in script

    @pytest.mark.parametrize("runner", ["sbatch", "bsub"])
    def test_an_extra_env_name_that_is_not_an_identifier_is_refused(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SECRET_KEY, SECRET_VALUE)
        with patch("shutil.which", return_value=f"/usr/bin/{runner}"), \
             patch("subprocess.run") as run:
            spec = _spec(tmp_path, runner_options={
                "partition": "n", "extra_env": {"X;rm -rf /": "1"},
            })
            result = (SlurmRunner() if runner == "sbatch" else LSFRunner()).run(spec)
        assert result.runner_status == "failed_setup"
        assert "shell identifiers" in (result.runner_error or "")
        run.assert_not_called()


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


class TestBatchSchedulersKeepValuesOffTheCommandLine:
    """No value a batch runner sends into the job may appear in a submit command's argv.

    ``argv`` is world-readable on the submit host: any user can read it with ``ps`` or
    ``/proc/<pid>/cmdline`` for as long as the submit command runs. SLURM used to put the
    whole job script, ``export OPENAI_API_KEY=…`` lines included, on ``sbatch``'s command
    line as ``--wrap <script>``; ``extra_env`` (ADR-0311) is how operators pass API keys,
    so the documented path disclosed them. The script now goes on the submit command's
    stdin, and nothing that carries a value is left on the shared filesystem.
    """

    EXTRA_KEY = "OPENAI_API_KEY"

    def _run(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> list[Any]:
        monkeypatch.setenv(SECRET_KEY, SECRET_VALUE)
        monkeypatch.setenv(NF_SECRET_KEY, "nf-token-must-never-be-transported")
        fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
        extra = {"extra_env": {self.EXTRA_KEY: SECRET_VALUE}}
        with patch("shutil.which", return_value=f"/usr/bin/{runner}"), \
             patch("subprocess.run", return_value=fail) as run:
            if runner == "sbatch":
                SlurmRunner().run(_spec(tmp_path, runner_options={"partition": "n", **extra}))
            elif runner == "bsub":
                LSFRunner().run(_spec(tmp_path, runner_options=extra))
            else:
                PBSRunner().run(_spec(tmp_path, runner_options={}))
        return list(run.call_args_list)

    @pytest.mark.parametrize("runner", ["sbatch", "bsub", "qsub"])
    def test_no_secret_value_appears_anywhere_in_argv(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._run(runner, tmp_path, monkeypatch)
        assert calls, f"{runner} was never invoked"
        for call in calls:
            argv = [str(a) for a in call.args[0]]
            for value in (SECRET_VALUE, "nf-token-must-never-be-transported",
                          "aws-must-never-be-transported"):
                assert not any(value in a for a in argv), (
                    f"{argv[0]} carries a secret on its command line, readable by every "
                    f"user on the submit host via ps or /proc/<pid>/cmdline: {argv}"
                )
            assert "--wrap" not in argv

    @pytest.mark.parametrize("runner", ["sbatch", "bsub"])
    def test_the_job_script_travels_on_stdin(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submit = self._run(runner, tmp_path, monkeypatch)[0]
        script = submit.kwargs.get("input")
        assert isinstance(script, bytes), f"{runner} got no job script on stdin"
        assert script.startswith(b"#!"), "sbatch/bsub need an interpreter line on stdin"
        # The operator's explicit extra_env still reaches the job (ADR-0311 decision 2).
        assert f"export {self.EXTRA_KEY}={SECRET_VALUE}".encode() in script
        assert b"NOVAFABRIC_SPAN_ID" in script

    @pytest.mark.parametrize("runner", ["sbatch", "bsub", "qsub"])
    def test_no_secret_value_is_left_on_the_shared_filesystem(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The capsule dir is on a shared FS, and it is sealed and exported as evidence."""
        self._run(runner, tmp_path, monkeypatch)
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert SECRET_VALUE.encode() not in path.read_bytes(), (
                    f"{runner} left a secret in {path.relative_to(tmp_path)}"
                )


def _fake_scheduler_bin(directory: Path, name: str, stdout: str) -> Path:
    """An executable stand-in that records its own argv and stdin, as the OS sees them."""
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$0" "$@" > "{directory}/{name}.argv"\n'
        f'cat > "{directory}/{name}.stdin"\n'
        f"printf '%s\\n' '{stdout}'\n"
    )
    path.chmod(0o700)
    return path


#: Values carrying every character a POSIX shell treats specially. The second has no
#: whitespace or quote, which the old quoter took as "safe" and left bare: its ``$HOME``
#: was expanded, and ``;`` / ``&`` / backticks ran as commands in the job.
NASTY_VALUES = [
    "s3cr3t $(id);`id` & | 'q' \"dq\" \\ * ~ end",
    "s3cr3t$HOME;`id`&|*~\\end",
]


class TestBatchScriptsSurviveARealShell:
    """End to end with real child processes: what ``/proc/<pid>/cmdline`` would show,
    and what the job's shell would actually export."""

    @pytest.mark.parametrize("value", NASTY_VALUES)
    def test_sbatch_argv_has_no_secret_and_the_script_reaches_stdin(
        self, tmp_path: Path, value: str
    ) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        sbatch = _fake_scheduler_bin(bindir, "sbatch", "42")
        scontrol = _fake_scheduler_bin(
            bindir, "scontrol", "JobId=42 JobState=COMPLETED ExitCode=0:0"
        )
        sacct = _fake_scheduler_bin(bindir, "sacct", "")
        spec = _spec(tmp_path, runner_options={
            "partition": "n", "extra_env": {"OPENAI_API_KEY": value},
            "poll_interval_s": 0.01,
        }, timeout_s=30.0)
        result = SlurmRunner(
            sbatch_bin=str(sbatch), sacct_bin=str(sacct), scontrol_bin=str(scontrol),
        ).run(spec)
        assert result.runner_status == "completed", result.runner_error
        argv = (bindir / "sbatch.argv").read_text()
        assert "s3cr3t" not in argv and "--wrap" not in argv, argv
        script = (bindir / "sbatch.stdin").read_text()
        assert script.startswith("#!/bin/sh\n")
        assert _exported_value(script, "OPENAI_API_KEY") == value, (
            "the job would see a mangled — or executed — value"
        )

    @pytest.mark.parametrize("value", NASTY_VALUES)
    def test_bsub_script_exports_the_value_verbatim(self, tmp_path: Path, value: str) -> None:
        fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
        with patch("subprocess.run", return_value=fail) as run:
            LSFRunner().run(_spec(
                tmp_path, runner_options={"extra_env": {"OPENAI_API_KEY": value}},
            ))
        script = run.call_args.kwargs["input"].decode()
        assert _exported_value(script, "OPENAI_API_KEY") == value


def _exported_value(script: str, name: str) -> str:
    """Run a job script's ``export`` lines in a real ``/bin/sh`` and read ``name`` back."""
    exports = "\n".join(
        line for line in script.splitlines() if line.startswith(f"export {name}=")
    )
    probe = f'{exports}\nprintf "%s" "${name}"\n'
    out = subprocess.run(["/bin/sh", "-c", probe], capture_output=True, check=True)
    return out.stdout.decode()
