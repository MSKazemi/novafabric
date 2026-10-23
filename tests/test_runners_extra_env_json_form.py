"""``--runner-option extra_env='{"K":"V"}'`` reaches the job, or fails loudly.

ADR-0270's migration note and the v0.102.0 upgrade notes both tell users to pass
``--runner-option extra_env='{"LOG_LEVEL":"debug"}'``. The CLI hands a runner that
as a plain string, and :func:`coerce_str_dict` only knew ``K=V,K2=V2``: it split the
JSON on ``,``, found no ``=`` in ``{"K":"V"}`` and **skipped** it, so the variable
silently never reached the container, pod or batch job.

A string option that starts with ``{`` is now parsed as a JSON object of string to
string. Anything else in that form — invalid JSON, a non-object, a non-string value,
an empty or duplicated name — is refused with :class:`RunnerOptionError`, and every
runner that takes ``extra_env`` turns that into ``failed_setup`` before it runs
anything. The names then go through the same rules as the ``K=V`` form (shell
identifiers for ``slurm``/``lsf``).
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from novafabric.cli.capture import _parse_runner_option
from novafabric.runners import DockerRunner, KubernetesRunner, RunnerJobSpec, SlurmRunner
from novafabric.runners._lsf import LSFRunner
from novafabric.runners._options import RunnerOptionError, coerce_str_dict

JSON_FORM = '{"LOG_LEVEL":"debug","HF_HOME":"/scratch/hf,a=b"}'
EXPECTED = {"LOG_LEVEL": "debug", "HF_HOME": "/scratch/hf,a=b"}


def _spec(tmp_path: Path, **runner_options: object) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTJSONEXTRAENV00000000",
        command=["python", "agent.py"],
        capsule_dir=capsule_dir,
        env={"NOVAFABRIC_CAPSULE_DIR": str(capsule_dir), "NOVAFABRIC_SPAN_ID": "0" * 16,
             "PATH": "/usr/bin:/bin"},
        runner_options=dict(runner_options),
    )


def _run(runner: str, tmp_path: Path, extra_env: object) -> tuple[Any, Any]:
    """Run ``runner`` until its first subprocess call, which fails; return both."""
    fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
    with patch("shutil.which", return_value=f"/usr/bin/{runner}"), \
         patch("subprocess.run", return_value=fail) as run:
        if runner == "docker":
            result = DockerRunner().run(_spec(tmp_path, image="img", extra_env=extra_env))
        elif runner == "kubectl":
            result = KubernetesRunner().run(
                _spec(tmp_path, image="img", namespace="ns", extra_env=extra_env)
            )
        elif runner == "sbatch":
            result = SlurmRunner().run(_spec(tmp_path, partition="n", extra_env=extra_env))
        else:
            result = LSFRunner().run(_spec(tmp_path, extra_env=extra_env))
    return result, run


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class TestCoerceStrDictJsonForm:
    def test_the_documented_json_form_is_parsed(self) -> None:
        assert coerce_str_dict('{"LOG_LEVEL":"debug"}') == {"LOG_LEVEL": "debug"}

    def test_commas_and_equals_inside_values_survive(self) -> None:
        assert coerce_str_dict(JSON_FORM) == EXPECTED

    def test_surrounding_whitespace_is_allowed(self) -> None:
        assert coerce_str_dict('  { "A" : "1" }  ') == {"A": "1"}

    def test_an_empty_object_is_empty(self) -> None:
        assert coerce_str_dict("{}") == {}

    def test_what_the_cli_parser_hands_the_runner_round_trips(self) -> None:
        key, value = _parse_runner_option(f"extra_env={JSON_FORM}")
        assert key == "extra_env"
        assert coerce_str_dict(value) == EXPECTED

    def test_the_key_value_form_is_unchanged(self) -> None:
        assert coerce_str_dict("A=1,B=x=y") == {"A": "1", "B": "x=y"}

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ('{"K":', "not valid JSON"),
            ("{K: V}", "not valid JSON"),
            ('{"K":"V"} trailing', "not valid JSON"),
            ('{"K":1}', "must be a string"),
            ('{"K":true}', "must be a string"),
            ('{"K":null}', "must be a string"),
            ('{"K":{"nested":"x"}}', "must be a string"),
            ('{"K":["a"]}', "must be a string"),
            ('{"":"v"}', "empty"),
            ('{"K":"1","K":"2"}', "more than once"),
        ],
    )
    def test_a_malformed_json_object_is_refused(self, raw: str, reason: str) -> None:
        with pytest.raises(RunnerOptionError, match=reason) as exc:
            coerce_str_dict(raw)
        assert isinstance(exc.value, ValueError), "callers catch ValueError"

    def test_pathologically_deep_nesting_is_refused_not_a_recursion_error(self) -> None:
        raw = '{"K":' + "[" * 100_000 + "]" * 100_000 + "}"
        with pytest.raises(RunnerOptionError, match="not valid JSON"):
            coerce_str_dict(raw)

    def test_the_value_is_never_echoed_in_the_error(self) -> None:
        """extra_env carries credentials; an error message must not leak one."""
        with pytest.raises(RunnerOptionError) as exc:
            coerce_str_dict('{"API_KEY":"sk-must-not-be-echoed", "N": 3}')
        assert "sk-must-not-be-echoed" not in str(exc.value)
        with pytest.raises(RunnerOptionError) as exc:
            coerce_str_dict('{"API_KEY":"sk-must-not-be-echoed"')
        assert "sk-must-not-be-echoed" not in str(exc.value)


# ---------------------------------------------------------------------------
# Every runner that takes extra_env
# ---------------------------------------------------------------------------


class TestEveryRunnerDeliversTheJsonForm:
    def test_kubernetes(self, tmp_path: Path) -> None:
        _, run = _run("kubectl", tmp_path, JSON_FORM)
        applied = json.loads(run.call_args.kwargs["input"].decode())
        envs = applied["spec"]["template"]["spec"]["containers"][0]["env"]
        for name, value in EXPECTED.items():
            assert {"name": name, "value": value} in envs

    def test_docker(self, tmp_path: Path) -> None:
        _, run = _run("docker", tmp_path, JSON_FORM)
        [docker_run] = [c for c in run.call_args_list if c.args[0][1] == "run"]
        argv = [str(a) for a in docker_run.args[0]]
        client_env = docker_run.kwargs["env"]
        for name, value in EXPECTED.items():
            assert name in argv, f"-e {name} missing from {argv}"
            assert client_env[name] == value
        assert not any("debug" in a for a in argv), "a value reached docker's argv"

    @pytest.mark.parametrize("runner", ["sbatch", "bsub"])
    def test_batch_schedulers(self, runner: str, tmp_path: Path) -> None:
        _, run = _run(runner, tmp_path, JSON_FORM)
        script = run.call_args.kwargs["input"].decode()
        for name, value in EXPECTED.items():
            assert f"export {name}={shlex.quote(value)}\n" in script
        assert run.call_args.kwargs["env"]["LOG_LEVEL"] == "debug"


class TestEveryRunnerRefusesAMalformedJsonForm:
    @pytest.mark.parametrize("runner", ["kubectl", "docker", "sbatch", "bsub"])
    @pytest.mark.parametrize("raw", ['{"K":', '{"K":1}'])
    def test_failed_setup_before_anything_runs(
        self, runner: str, raw: str, tmp_path: Path
    ) -> None:
        result, run = _run(runner, tmp_path, raw)
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 127
        assert "extra_env" in (result.runner_error or "")
        run.assert_not_called()

    @pytest.mark.parametrize("runner", ["sbatch", "bsub"])
    def test_json_names_follow_the_same_shell_identifier_rule(
        self, runner: str, tmp_path: Path
    ) -> None:
        result, run = _run(runner, tmp_path, '{"X;rm -rf /":"1"}')
        assert result.runner_status == "failed_setup"
        assert "shell identifiers" in (result.runner_error or "")
        run.assert_not_called()

    def test_kubernetes_node_selector_json_is_refused_too(self, tmp_path: Path) -> None:
        fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
        with patch("subprocess.run", return_value=fail) as run:
            result = KubernetesRunner().run(
                _spec(tmp_path, image="img", namespace="ns", node_selector='{"zone":')
            )
        assert result.runner_status == "failed_setup"
        assert "node_selector" in (result.runner_error or "")
        run.assert_not_called()
