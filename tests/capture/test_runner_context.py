"""ADR-0307 — capsules record the runner, the resolved image digest and the
Slurm job context (public issue #157).

Before this, a ``--runner docker`` capsule and a ``python payload.py`` capsule
differed only in their outputs, and a Slurm job's id reached the capsule only if
the payload printed it. The invariants pinned here:

* every ``nova capture`` capsule names its runner (``host.runner.name``);
* a container runner records the image **digest the runtime resolved**, or an
  explicit ``unresolved_reason`` — never a digest inferred from the tag;
* Slurm context comes from an explicit allow-list of ``SLURM_*`` variables,
  node names are hashed like the hostname, and nothing else crosses;
* the new fields are inside ``capsule.yaml``, so the secret scanner redacts them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture.env import (
    SLURM_ENV_ALLOWLIST,
    host_info,
    node_list_hash,
    runner_context,
    slurm_context_from_env,
    slurm_context_from_runner,
)
from novafabric.capture.orchestrator import _host_block
from novafabric.cli.main import app
from novafabric.runners import KubernetesRunner, RunnerJobSpec
from novafabric.runners._image import (
    IMAGE_PROVENANCE_KEY,
    ImageInspection,
    parse_docker_inspect,
    parse_kubernetes_image_id,
    resolve_docker_image,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "capsule-runner-context"
SCHEMA_PATHS = [
    REPO_ROOT / "schemas" / "run-capsule.schema.json",
    REPO_ROOT / "src" / "novafabric" / "schemas" / "run-capsule.schema.json",
]
PACKAGED_SCHEMA = SCHEMA_PATHS[1]

ID = "sha256:" + "a" * 64
ID2 = "sha256:" + "c" * 64
REPO = "docker.io/library/python@sha256:" + "b" * 64


def _validator(schema_path: Path) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(schema_path.read_text()))


# ── Schema: golden fixtures against both schema copies ──────────────────────

_FIXTURES = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))


def test_there_are_fixtures_of_both_kinds() -> None:
    assert sum(f.startswith("valid-") for f in _FIXTURES) >= 5, _FIXTURES
    assert sum(f.startswith("invalid-") for f in _FIXTURES) >= 8, _FIXTURES


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=["root", "packaged"])
@pytest.mark.parametrize("fixture", _FIXTURES)
def test_golden_fixture_behaves_as_named(schema_path: Path, fixture: str) -> None:
    manifest = json.loads((FIXTURE_DIR / fixture).read_text())
    errors = list(_validator(schema_path).iter_errors(manifest))
    if fixture.startswith("valid-"):
        assert not errors, [e.message for e in errors]
    else:
        assert errors, f"{fixture}: expected schema rejection, got none"


def test_the_host_block_is_identical_in_every_schema_copy() -> None:
    hosts = [json.loads(p.read_text())["$defs"]["Host"] for p in SCHEMA_PATHS]
    ui = REPO_ROOT / "ui" / "dashboard" / "src" / "data" / "schemas" / "run-capsule.schema.json"
    hosts.append(json.loads(ui.read_text())["$defs"]["Host"])
    assert hosts[0] == hosts[1] == hosts[2]


# ── Docker image inspection: parse and combine ───────────────────────────────


class TestParseDockerInspect:
    def test_id_and_repo_digests(self) -> None:
        got = parse_docker_inspect(f"{ID} {REPO}\n")
        assert got == ImageInspection(image_id=ID, repo_digests=(REPO,))

    def test_locally_built_image_has_no_repo_digest(self) -> None:
        assert parse_docker_inspect(ID) == ImageInspection(image_id=ID)

    @pytest.mark.parametrize("out", ["", "   \n", "a" * 64, "python:3.12-slim", "<no value>"])
    def test_anything_but_a_sha256_id_is_refused(self, out: str) -> None:
        got = parse_docker_inspect(out)
        assert not got.resolved
        assert got.error

    def test_malformed_and_duplicate_repo_digests_are_dropped(self) -> None:
        got = parse_docker_inspect(f"{ID} {REPO} not-a-digest {REPO} x@sha256:zz")
        assert got.repo_digests == (REPO,)


class TestResolveDockerImage:
    def test_before_wins_when_both_agree(self) -> None:
        r = resolve_docker_image(ImageInspection(image_id=ID), ImageInspection(image_id=ID))
        assert r == {"resolved_by": "docker-image-inspect", "image_id": ID}

    def test_before_stands_when_after_fails(self) -> None:
        r = resolve_docker_image(
            ImageInspection(image_id=ID, repo_digests=(REPO,)),
            ImageInspection(error="gone"),
        )
        assert r == {
            "resolved_by": "docker-image-inspect", "image_id": ID, "repo_digests": [REPO],
        }

    def test_after_is_used_when_the_image_was_pulled_by_the_run(self) -> None:
        r = resolve_docker_image(
            ImageInspection(error="No such image"), ImageInspection(image_id=ID)
        )
        assert r["image_id"] == ID

    def test_a_tag_moved_during_the_run_records_no_digest(self) -> None:
        r = resolve_docker_image(ImageInspection(image_id=ID), ImageInspection(image_id=ID2))
        assert set(r) == {"unresolved_reason"}
        assert ID in r["unresolved_reason"] and ID2 in r["unresolved_reason"]

    def test_neither_resolved_records_the_runtime_error(self) -> None:
        r = resolve_docker_image(
            ImageInspection(error="first"), ImageInspection(error="No such image: x")
        )
        assert r == {"unresolved_reason": "No such image: x"}


class TestParseKubernetesImageId:
    def test_containerd_repo_digest(self) -> None:
        assert parse_kubernetes_image_id(REPO) == {
            "resolved_by": "kubernetes-pod-status", "repo_digests": [REPO],
        }

    def test_dockershim_pullable_prefix_is_stripped(self) -> None:
        assert parse_kubernetes_image_id(f"docker-pullable://{REPO}")["repo_digests"] == [REPO]

    def test_dockershim_local_image_id(self) -> None:
        assert parse_kubernetes_image_id(f"docker://{ID}") == {
            "resolved_by": "kubernetes-pod-status", "image_id": ID,
        }

    @pytest.mark.parametrize("raw", ["", "  ", "python:3.12", "docker://abc"])
    def test_anything_else_is_unresolved(self, raw: str) -> None:
        assert set(parse_kubernetes_image_id(raw)) == {"unresolved_reason"}


# ── Slurm context: the allow-list ────────────────────────────────────────────

_FULL_SLURM_ENV = {
    "SLURM_JOB_ID": "424242",
    "SLURM_ARRAY_JOB_ID": "424240",
    "SLURM_ARRAY_TASK_ID": "2",
    "SLURM_JOB_PARTITION": "gpu",
    "SLURM_CLUSTER_NAME": "cluster-x",
    "SLURM_JOB_NODELIST": "node[01-02]",
    "SLURM_JOB_NUM_NODES": "2",
}


class TestSlurmContextFromEnv:
    def test_no_job_id_means_no_block(self) -> None:
        assert slurm_context_from_env({"SLURM_JOB_PARTITION": "gpu"}) is None
        assert slurm_context_from_env({}) is None

    def test_every_allow_listed_field(self) -> None:
        assert slurm_context_from_env(_FULL_SLURM_ENV) == {
            "job_id": "424242",
            "array_job_id": "424240",
            "array_task_id": "2",
            "partition": "gpu",
            "cluster": "cluster-x",
            "node_list_hash": node_list_hash("node[01-02]"),
            "node_count": 2,
            "source": "environment",
        }

    def test_older_jobid_spelling(self) -> None:
        assert slurm_context_from_env({"SLURM_JOBID": "9"}) == {
            "job_id": "9", "source": "environment",
        }

    def test_the_node_list_is_hashed_never_recorded(self) -> None:
        block = slurm_context_from_env(_FULL_SLURM_ENV)
        assert block is not None
        assert "node" not in json.dumps(block).replace("node_", "")

    @pytest.mark.parametrize(
        "override",
        [
            {"SLURM_JOB_ID": "12a"},
            {"SLURM_JOB_ID": ""},
            {"SLURM_JOB_ID": "-1"},
        ],
    )
    def test_a_malformed_job_id_means_no_block(self, override: dict[str, str]) -> None:
        assert slurm_context_from_env({**_FULL_SLURM_ENV, **override}) is None

    def test_malformed_optional_values_are_left_out_not_repaired(self) -> None:
        block = slurm_context_from_env({
            "SLURM_JOB_ID": "1",
            "SLURM_ARRAY_TASK_ID": "two",
            "SLURM_JOB_PARTITION": "gpu cpu",
            "SLURM_CLUSTER_NAME": "x" * 200,
            "SLURM_JOB_NUM_NODES": "0",
        })
        assert block == {"job_id": "1", "source": "environment"}

    def test_variables_outside_the_allow_list_never_cross(self) -> None:
        env = {
            **_FULL_SLURM_ENV,
            "SLURM_JOB_ACCOUNT": "acct-do-not-record",
            "SLURM_SUBMIT_DIR": "/home/someone/do-not-record",
            "SLURM_EXPORT_ENV": "do-not-record",
            "SLURMD_NODENAME": "node-do-not-record",
        }
        assert "do-not-record" not in json.dumps(slurm_context_from_env(env))
        assert "SLURM_JOB_ACCOUNT" not in SLURM_ENV_ALLOWLIST
        assert all(v.startswith("SLURM_") for v in SLURM_ENV_ALLOWLIST)

    def test_host_info_carries_it_unless_told_not_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in _FULL_SLURM_ENV.items():
            monkeypatch.setenv(key, value)
        assert host_info()["slurm"]["job_id"] == "424242"
        assert "slurm" not in host_info(scheduler_context=False)

    def test_host_info_is_unchanged_outside_slurm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in SLURM_ENV_ALLOWLIST:
            monkeypatch.delenv(key, raising=False)
        assert "slurm" not in host_info()


class TestSlurmContextFromRunner:
    def test_job_partition_and_cluster(self) -> None:
        assert slurm_context_from_runner(
            {"job_id": "77", "partition": "gpu", "cluster": "c1", "final_state": "COMPLETED"}
        ) == {"job_id": "77", "partition": "gpu", "cluster": "c1", "source": "runner"}

    def test_a_multi_partition_request_does_not_name_a_partition(self) -> None:
        assert "partition" not in (
            slurm_context_from_runner({"job_id": "77", "partition": "gpu,cpu"}) or {}
        )

    def test_no_job_id_no_block(self) -> None:
        assert slurm_context_from_runner({"partition": "gpu"}) is None


# ── host.runner assembly ─────────────────────────────────────────────────────


class _NamedRunner:
    def __init__(self, name: str) -> None:
        self.name = name


class TestHostBlock:
    def test_only_the_name_and_image_cross_from_runner_metadata(self) -> None:
        image = {"reference": "x:1", "image_id": ID, "resolved_by": "docker-image-inspect"}
        block = runner_context(
            "docker", {"image": "x:1", "container_id": "abc", IMAGE_PROVENANCE_KEY: image}
        )
        assert block == {"name": "docker", "image": image}

    def test_slurm_runner_replaces_the_environment_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`nova capture --runner slurm` inside an salloc: the job that ran the
        workload is the one sbatch created, not the allocation nova ran in."""
        monkeypatch.setenv("SLURM_JOB_ID", "1000")
        host = _host_block(_NamedRunner("slurm"), {"job_id": "1001", "partition": "gpu"})  # type: ignore[arg-type]
        assert host["slurm"] == {"job_id": "1001", "partition": "gpu", "source": "runner"}
        assert host["runner"] == {"name": "slurm"}

    def test_slurm_runner_without_a_job_records_no_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLURM_JOB_ID", "1000")
        host = _host_block(_NamedRunner("slurm"), {})  # type: ignore[arg-type]
        assert "slurm" not in host

    def test_other_runners_keep_the_environment_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLURM_JOB_ID", "1000")
        host = _host_block(_NamedRunner("docker"), {})  # type: ignore[arg-type]
        assert host["slurm"] == {"job_id": "1000", "source": "environment"}
        _validator(PACKAGED_SCHEMA).validate({"host": host} | _minimal_manifest())


def _minimal_manifest() -> dict[str, Any]:
    base = json.loads((FIXTURE_DIR / "valid-local-runner.json").read_text())
    base.pop("host")
    return base


# ── Kubernetes: imageID from the pod query the runner already makes ─────────


def _k8s_spec(tmp_path: Path) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir()
    return RunnerJobSpec(
        run_id="01TESTK8S00000000000000000",
        command=["python", "x.py"],
        capsule_dir=capsule_dir,
        env={"NOVAFABRIC_CAPSULE_DIR": str(capsule_dir)},
        runner_options={"image": "myorg/agent:1.2", "namespace": "ns"},
    )


def _k8s_calls(pods_stdout: bytes, pods_rc: int = 0) -> list[subprocess.CompletedProcess[bytes]]:
    ok = subprocess.CompletedProcess([], 0, b"", b"")
    return [
        subprocess.CompletedProcess([], 0, b"job/x created\n", b""),
        subprocess.CompletedProcess([], 0, json.dumps({"status": {"succeeded": 1}}).encode(), b""),
        subprocess.CompletedProcess([], pods_rc, pods_stdout, b""),
        ok, ok, ok,  # logs, cp, delete
    ]


class TestKubernetesImageDigest:
    def test_pod_status_image_id_is_recorded(self, tmp_path: Path) -> None:
        calls = _k8s_calls(f"my-pod\t{REPO}".encode())
        with patch("subprocess.run", side_effect=calls) as run:
            result = KubernetesRunner().run(_k8s_spec(tmp_path))
        assert result.runner_metadata["pod_name"] == "my-pod"
        assert result.runner_metadata[IMAGE_PROVENANCE_KEY] == {
            "reference": "myorg/agent:1.2",
            "resolved_by": "kubernetes-pod-status",
            "repo_digests": [REPO],
        }
        pods_argv = run.call_args_list[2].args[0]
        assert pods_argv[:3] == ["kubectl", "get", "pods"], pods_argv
        assert "imageID" in pods_argv[-1]

    def test_no_image_id_in_the_pod_status_is_unresolved(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=_k8s_calls(b"my-pod\t")):
            result = KubernetesRunner().run(_k8s_spec(tmp_path))
        assert result.runner_metadata["pod_name"] == "my-pod"
        block = result.runner_metadata[IMAGE_PROVENANCE_KEY]
        assert set(block) == {"reference", "unresolved_reason"}

    def test_no_pod_is_unresolved(self, tmp_path: Path) -> None:
        calls = _k8s_calls(b"", pods_rc=1)[:3] + [subprocess.CompletedProcess([], 0, b"", b"")]
        with patch("subprocess.run", side_effect=calls):
            result = KubernetesRunner().run(_k8s_spec(tmp_path))
        assert "pod_name" not in result.runner_metadata
        assert result.runner_metadata[IMAGE_PROVENANCE_KEY]["unresolved_reason"] == (
            "the workload pod was not found"
        )


# ── End to end through `nova capture` ────────────────────────────────────────

_STUB_DOCKER = """#!/bin/sh
if [ "$1" = image ] && [ "$2" = inspect ]; then
  if [ -n "${STUB_INSPECT_FAIL:-}" ]; then
    echo "Error response from daemon: No such image: $5" >&2
    exit 1
  fi
  if [ -n "${STUB_INSPECT_COUNTER:-}" ]; then
    n=$(cat "$STUB_INSPECT_COUNTER" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$STUB_INSPECT_COUNTER"
    if [ "$n" -gt 0 ]; then
      echo "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      exit 0
    fi
  fi
  echo "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa docker.io/library/python@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  exit 0
fi
if [ "$1" = run ]; then
  echo "stub: docker run"
  exit 0
fi
exit 2
"""


def _capture(tmp_path: Path, args: list[str], env: dict[str, str]) -> dict[str, Any]:
    out = tmp_path / "capsules"
    out.mkdir(exist_ok=True)
    result = CliRunner().invoke(app, ["capture", "--output-dir", str(out), *args], env=env)
    assert result.exit_code == 0, result.output
    capsules = [d for d in out.iterdir() if (d / "capsule.yaml").is_file()]
    assert len(capsules) == 1, capsules
    validated = CliRunner().invoke(app, ["validate", str(capsules[0])])
    assert validated.exit_code == 0, validated.output
    manifest: dict[str, Any] = yaml.safe_load((capsules[0] / "capsule.yaml").read_text())
    _validator(PACKAGED_SCHEMA).validate(manifest)
    return manifest


def _no_slurm_env() -> dict[str, str]:
    # CliRunner(env=) cannot unset; None removes a key for the duration.
    return {k: None for k in os.environ if k.startswith("SLURM")}  # type: ignore[misc]


@pytest.fixture()
def stub_docker(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(_STUB_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    return {**_no_slurm_env(), "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}


_DOCKER_ARGS = ["--runner", "docker", "--runner-option", "image=python:3.12-slim",
                "--", "python", "/work/payload.py"]


def test_a_local_capture_names_its_runner(tmp_path: Path) -> None:
    manifest = _capture(
        tmp_path, ["--", sys.executable, "-c", "print(1)"], _no_slurm_env()
    )
    assert manifest["host"]["runner"] == {"name": "local"}
    assert "slurm" not in manifest["host"]


def test_a_docker_capture_records_the_resolved_digest(
    tmp_path: Path, stub_docker: dict[str, str]
) -> None:
    manifest = _capture(tmp_path, _DOCKER_ARGS, stub_docker)
    assert manifest["host"]["runner"] == {
        "name": "docker",
        "image": {
            "reference": "python:3.12-slim",
            "resolved_by": "docker-image-inspect",
            "image_id": ID,
            "repo_digests": [REPO],
        },
    }


def test_a_docker_capture_whose_digest_cannot_be_resolved_says_why(
    tmp_path: Path, stub_docker: dict[str, str]
) -> None:
    manifest = _capture(tmp_path, _DOCKER_ARGS, {**stub_docker, "STUB_INSPECT_FAIL": "1"})
    image = manifest["host"]["runner"]["image"]
    assert set(image) == {"reference", "unresolved_reason"}
    assert "No such image" in image["unresolved_reason"]


def test_a_tag_re_pointed_during_the_run_records_no_digest(
    tmp_path: Path, stub_docker: dict[str, str]
) -> None:
    counter = tmp_path / "inspect-count"
    manifest = _capture(
        tmp_path, _DOCKER_ARGS, {**stub_docker, "STUB_INSPECT_COUNTER": str(counter)}
    )
    assert counter.read_text().strip() == "2", "expected one inspect before, one after"
    image = manifest["host"]["runner"]["image"]
    assert "image_id" not in image
    assert "cannot be attributed" in image["unresolved_reason"]


def test_a_capture_inside_a_slurm_job_records_the_job(tmp_path: Path) -> None:
    manifest = _capture(
        tmp_path,
        ["--", sys.executable, "-c", "print(1)"],
        {**_no_slurm_env(), **_FULL_SLURM_ENV, "SLURMD_NODENAME": "node-7"},
    )
    assert manifest["host"]["slurm"] == slurm_context_from_env(_FULL_SLURM_ENV)
    text = yaml.safe_dump(manifest)
    assert "node[01-02]" not in text and "node-7" not in text


def test_the_secret_scanner_sees_the_new_fields(tmp_path: Path) -> None:
    """The fields live in capsule.yaml, which is redacted before it is written."""
    key = "AKIAQWERTYUIOPASDFGH"
    manifest = _capture(
        tmp_path,
        ["--", sys.executable, "-c", "print(1)"],
        {**_no_slurm_env(), "SLURM_JOB_ID": "5", "SLURM_CLUSTER_NAME": key},
    )
    assert slurm_context_from_env({"SLURM_JOB_ID": "5", "SLURM_CLUSTER_NAME": key}) == {
        "job_id": "5", "cluster": key, "source": "environment",
    }, "the key must reach the manifest builder, or this test proves nothing"
    assert manifest["host"]["slurm"]["job_id"] == "5"
    assert manifest["host"]["slurm"]["cluster"] != key  # present, and redacted
    assert key not in yaml.safe_dump(manifest)
