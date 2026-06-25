"""Unit tests for the KubernetesRunner (ADR-0025 Runners.3).

Mocks subprocess.run so they pass without a kubectl binary or a
cluster. Live-cluster integration tests are out of scope for CI.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.runners import KubernetesRunner, RunnerJobSpec, RunnerSpec
from novafabric.runners._kubernetes import _build_job_manifest


def _spec(
    tmp_path: Path, command: list[str], **kwargs: object
) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTK8S00000000000000000",
        command=command,
        capsule_dir=capsule_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
            "PATH": "/usr/bin",
        },
        **kwargs,  # type: ignore[arg-type]
    )


class TestKubernetesRunnerProtocol:
    def test_satisfies_runner_spec(self) -> None:
        runner: RunnerSpec = KubernetesRunner()
        assert runner.name == "kubernetes"
        assert "Kubernetes" in runner.description or "kubernetes" in runner.description.lower()


class TestKubernetesRunnerSupports:
    def test_returns_false_when_kubectl_not_on_path(self) -> None:
        with patch("shutil.which", return_value=None):
            ok, why = KubernetesRunner().supports()
        assert ok is False
        assert "not found" in why

    def test_returns_false_when_cluster_unreachable(self) -> None:
        client_ok = subprocess.CompletedProcess([], 0, b"{}", b"")
        cluster_fail = subprocess.CompletedProcess(
            [], 1, b"", b"Unable to connect to the server\n",
        )
        with patch("shutil.which", return_value="/usr/local/bin/kubectl"), \
             patch("subprocess.run", side_effect=[client_ok, cluster_fail]):
            ok, why = KubernetesRunner().supports()
        assert ok is False
        assert "cluster" in why.lower() or "kubeconfig" in why.lower()

    def test_returns_true_when_cluster_reachable(self) -> None:
        client_ok = subprocess.CompletedProcess([], 0, b"{}", b"")
        cluster_ok = subprocess.CompletedProcess(
            [], 0, b"Kubernetes control plane is running at https://...\n", b"",
        )
        with patch("shutil.which", return_value="/usr/local/bin/kubectl"), \
             patch("subprocess.run", side_effect=[client_ok, cluster_ok]):
            ok, why = KubernetesRunner().supports()
        assert ok is True
        assert why == ""


class TestKubernetesRunnerErrorPaths:
    def test_missing_image_returns_failed_setup(self, tmp_path: Path) -> None:
        result = KubernetesRunner().run(_spec(
            tmp_path, ["python"], runner_options={"namespace": "default"},
        ))
        assert result.exit_code == 127
        assert result.runner_status == "failed_setup"
        assert "image" in (result.runner_error or "").lower()

    def test_missing_namespace_returns_failed_setup(self, tmp_path: Path) -> None:
        result = KubernetesRunner().run(_spec(
            tmp_path, ["python"], runner_options={"image": "x"},
        ))
        assert result.exit_code == 127
        assert result.runner_status == "failed_setup"
        assert "namespace" in (result.runner_error or "").lower()

    def test_apply_failure_returns_failed_setup(self, tmp_path: Path) -> None:
        apply_fail = subprocess.CompletedProcess(
            [], 1, b"", b"the namespace doesn't exist\n",
        )
        with patch("subprocess.run", return_value=apply_fail):
            result = KubernetesRunner().run(_spec(
                tmp_path, ["python", "x"],
                runner_options={"image": "myorg/img", "namespace": "ns"},
            ))
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 125


class TestKubernetesRunnerSuccessPath:
    def test_completed_job_returns_completed(self, tmp_path: Path) -> None:
        apply_ok = subprocess.CompletedProcess([], 0, b"job/x created\n", b"")
        # Job status: succeeded.
        job_status = subprocess.CompletedProcess(
            [], 0,
            json.dumps({"status": {"succeeded": 1}}).encode(),
            b"",
        )
        # Pod listing.
        pods = subprocess.CompletedProcess([], 0, b"my-pod-abc", b"")
        # kubectl logs.
        logs = subprocess.CompletedProcess([], 0, b"hello from pod\n", b"")
        # kubectl cp.
        cp = subprocess.CompletedProcess([], 0, b"", b"")
        # kubectl delete.
        delete = subprocess.CompletedProcess([], 0, b"job deleted\n", b"")

        with patch("subprocess.run", side_effect=[
            apply_ok, job_status, pods, logs, cp, delete,
        ]):
            result = KubernetesRunner().run(_spec(
                tmp_path, ["python", "x.py"],
                runner_options={"image": "myorg/img", "namespace": "ns"},
            ))
        assert result.runner_status == "completed"
        assert result.exit_code == 0
        assert b"hello from pod" in result.stdout
        assert result.runner_metadata.get("namespace") == "ns"
        assert result.runner_metadata.get("pod_name") == "my-pod-abc"

    def test_failed_job_returns_completed_with_nonzero(self, tmp_path: Path) -> None:
        apply_ok = subprocess.CompletedProcess([], 0, b"", b"")
        job_status = subprocess.CompletedProcess(
            [], 0,
            json.dumps({"status": {"failed": 1}}).encode(),
            b"",
        )
        pods = subprocess.CompletedProcess([], 0, b"pod1", b"")
        logs = subprocess.CompletedProcess([], 0, b"", b"workload failed\n")
        cp = subprocess.CompletedProcess([], 0, b"", b"")
        delete = subprocess.CompletedProcess([], 0, b"", b"")

        with patch("subprocess.run", side_effect=[
            apply_ok, job_status, pods, logs, cp, delete,
        ]):
            result = KubernetesRunner().run(_spec(
                tmp_path, ["python", "x"],
                runner_options={"image": "i", "namespace": "n"},
            ))
        assert result.runner_status == "completed"
        assert result.exit_code == 1


class TestJobManifest:
    """Direct tests of the manifest builder so anti-pattern enforcement
    is verified at the JSON level without round-tripping through kubectl."""

    def test_no_privileged_security_context(self) -> None:
        manifest = _build_job_manifest(
            job_name="x", namespace="ns", image="i", command=["echo"],
            env={"K": "V"}, in_pod_capsule_dir="/c",
            service_account=None, node_selector=None, resources=None,
        )
        sec = manifest["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        assert sec["privileged"] is False
        assert sec["allowPrivilegeEscalation"] is False

    def test_no_host_namespaces(self) -> None:
        manifest = _build_job_manifest(
            job_name="x", namespace="ns", image="i", command=["echo"],
            env={}, in_pod_capsule_dir="/c",
            service_account=None, node_selector=None, resources=None,
        )
        pod_spec = manifest["spec"]["template"]["spec"]
        assert pod_spec["hostNetwork"] is False
        assert pod_spec["hostPID"] is False
        assert pod_spec["hostIPC"] is False

    def test_backoff_limit_zero(self) -> None:
        """Capsule semantics require one shot — no retries (otherwise we'd
        get multiple capsules for the same run_id)."""
        manifest = _build_job_manifest(
            job_name="x", namespace="ns", image="i", command=["echo"],
            env={}, in_pod_capsule_dir="/c",
            service_account=None, node_selector=None, resources=None,
        )
        assert manifest["spec"]["backoffLimit"] == 0

    def test_service_account_only_set_when_provided(self) -> None:
        m_default = _build_job_manifest(
            job_name="x", namespace="ns", image="i", command=["echo"],
            env={}, in_pod_capsule_dir="/c",
            service_account=None, node_selector=None, resources=None,
        )
        assert "serviceAccountName" not in m_default["spec"]["template"]["spec"]

        m_explicit = _build_job_manifest(
            job_name="x", namespace="ns", image="i", command=["echo"],
            env={}, in_pod_capsule_dir="/c",
            service_account="agent-runner", node_selector=None, resources=None,
        )
        assert m_explicit["spec"]["template"]["spec"]["serviceAccountName"] == "agent-runner"

    def test_env_vars_passed_as_list_of_dicts(self) -> None:
        manifest = _build_job_manifest(
            job_name="x", namespace="ns", image="i", command=["echo"],
            env={"NOVAFABRIC_SPAN_ID": "abc", "K": "V"},
            in_pod_capsule_dir="/c",
            service_account=None, node_selector=None, resources=None,
        )
        envs = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {"name": "NOVAFABRIC_SPAN_ID", "value": "abc"} in envs
        assert {"name": "K", "value": "V"} in envs

    def test_capsule_volume_is_emptydir(self) -> None:
        manifest = _build_job_manifest(
            job_name="x", namespace="ns", image="i", command=["echo"],
            env={}, in_pod_capsule_dir="/novafabric/capsule",
            service_account=None, node_selector=None, resources=None,
        )
        vols = manifest["spec"]["template"]["spec"]["volumes"]
        assert any(v["name"] == "capsule" and "emptyDir" in v for v in vols)


@pytest.mark.skipif(
    os.environ.get("NOVAFABRIC_TEST_LIVE_KUBERNETES", "") != "1",
    reason="opt-in: set NOVAFABRIC_TEST_LIVE_KUBERNETES=1 to run against a real cluster",
)
class TestKubernetesRunnerLiveSmoke:
    """Opt-in. Requires a reachable cluster + a namespace where the
    executing principal can create/get/delete jobs and pods."""

    def test_alpine_echo_succeeds_in_default_namespace(self, tmp_path: Path) -> None:
        result = KubernetesRunner().run(_spec(
            tmp_path, ["echo", "hello-from-job"],
            runner_options={"image": "alpine:3.20", "namespace": "default"},
            timeout_s=120.0,
        ))
        stderr_tail = result.stderr.decode(errors="replace")[:500]
        assert result.exit_code == 0, (
            f"runner_status={result.runner_status} stderr={stderr_tail}"
        )
