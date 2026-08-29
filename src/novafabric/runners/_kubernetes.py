"""Kubernetes runner (ADR-0025 Runners.3).

Runs the captured workload as a Kubernetes ``Job``. Uses ``kubectl``
shell-out (not the Python kubernetes client) to keep the runtime
dependency surface minimal — no in-cluster setup required, no extra
deps in pyproject.toml. Capsule artifacts are pulled back to the
local filesystem via ``kubectl cp`` after completion.

Image responsibility (per ADR-0025): the user supplies an image with
NovaFabric installed.

RBAC requirement: the executing principal needs at least
``create``, ``get``, ``watch``, ``delete`` on ``jobs`` and ``pods``
in the target namespace, plus ``get`` on ``pods/log`` and
``pods/exec`` for ``kubectl cp``. No cluster-admin needed.

Anti-patterns explicitly forbidden by this runner (per ADR-0025
§Anti-patterns):
  - No privileged pods (``securityContext.privileged: true``).
  - No host namespaces (``hostNetwork``, ``hostPID``, ``hostIPC``).
  - No ``serviceAccount: cluster-admin`` defaults.
  - No hardcoded image registries.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from novafabric.runners._options import coerce_str_dict
from novafabric.runners._poll import jittered_sleep
from novafabric.runners._types import RunnerJobResult, RunnerJobSpec

_DEFAULT_IN_POD_CAPSULE = "/novafabric/capsule"


# Accepts the CLI's "k=v,k2=v2" string as well as a real mapping — see
# novafabric.runners._options.
_coerce_str_dict = coerce_str_dict


def _build_job_manifest(
    *,
    job_name: str,
    namespace: str,
    image: str,
    command: list[str],
    env: dict[str, str],
    in_pod_capsule_dir: str,
    service_account: str | None,
    node_selector: dict[str, str] | None,
    resources: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a minimal Job manifest. The pod runs the workload, mounts
    an ``emptyDir`` for the capsule (so the in-pod capsule path is
    writable), and exits when the workload exits. ``kubectl cp`` later
    syncs the emptyDir contents back to the local FS."""
    container: dict[str, Any] = {
        "name": "workload",
        "image": image,
        "command": command,
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "volumeMounts": [{"name": "capsule", "mountPath": in_pod_capsule_dir}],
        "imagePullPolicy": "IfNotPresent",
        # Anti-pattern enforcement: explicit non-privileged security
        # context (overrideable by the cluster's PSP/PSS but not by us).
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "privileged": False,
            "readOnlyRootFilesystem": False,
            "runAsNonRoot": False,  # image-dependent; left to the user's image
        },
    }
    if resources:
        container["resources"] = resources

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "containers": [container],
        "volumes": [{"name": "capsule", "emptyDir": {}}],
        # Anti-pattern enforcement: never enable host namespaces.
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
    }
    if service_account:
        pod_spec["serviceAccountName"] = service_account
    if node_selector:
        pod_spec["nodeSelector"] = node_selector

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,  # one shot; no retries (capsule semantics)
            "template": {"spec": pod_spec},
        },
    }


class KubernetesRunner:
    """Run the captured workload as a Kubernetes Job.

    Required ``runner_options``:

    - ``image`` (str) — fully-qualified image reference.
    - ``namespace`` (str) — target namespace; must exist with RBAC for
      jobs/pods/pods-log/pods-exec.

    Optional ``runner_options``:

    - ``service_account`` (str) — pod's serviceAccountName. Recommended
      for least-privilege (default cluster ServiceAccount otherwise).
    - ``node_selector`` (dict[str, str]) — node selector labels.
    - ``resources`` (dict) — container resources block,
      e.g. ``{"requests": {"cpu": "1", "memory": "2Gi"},
              "limits": {"cpu": "2", "memory": "4Gi"}}``.
    - ``poll_interval_s`` (float) — Job-status poll interval. Default 2.0.
    """

    name: str = "kubernetes"
    description: str = (
        "Run the captured workload as a Kubernetes Job. "
        "Requires runner_options['image'] and ['namespace']."
    )

    def __init__(self, *, kubectl_bin: str = "kubectl") -> None:
        self._kubectl = kubectl_bin

    def supports(self) -> tuple[bool, str]:
        if shutil.which(self._kubectl) is None:
            return False, f"`{self._kubectl}` binary not found on PATH"
        # `kubectl version --client` exits 0 if kubectl works, regardless
        # of whether the cluster is reachable. We check cluster reachability
        # separately so the error message is more useful.
        try:
            client = subprocess.run(
                [self._kubectl, "version", "--client", "--output=json"],
                capture_output=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"could not run `{self._kubectl} version`: {exc}"
        if client.returncode != 0:
            stderr_msg = client.stderr.decode(errors="replace").strip()
            return False, f"`{self._kubectl}` is not usable: {stderr_msg}"

        try:
            cluster = subprocess.run(
                [self._kubectl, "cluster-info"],
                capture_output=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"could not probe cluster: {exc}"
        if cluster.returncode != 0:
            return False, "kubectl found but cluster not reachable (check kubeconfig)"
        return True, ""

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        opts = spec.runner_options or {}
        image = opts.get("image") if isinstance(opts.get("image"), str) else None
        namespace = opts.get("namespace") if isinstance(opts.get("namespace"), str) else None

        if not image:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                runner_error=(
                    "KubernetesRunner requires runner_options['image']. "
                    "Pass --runner-option image=<ref>."
                ),
            )
        if not namespace:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                runner_error=(
                    "KubernetesRunner requires runner_options['namespace']. "
                    "Pass --runner-option namespace=<ns>."
                ),
            )

        in_pod_capsule = _DEFAULT_IN_POD_CAPSULE
        env = dict(spec.env)
        env["NOVAFABRIC_CAPSULE_DIR"] = in_pod_capsule

        # Job name is derived from run_id (kubernetes name rules: lowercase,
        # alphanumerics + hyphens, max 63 chars).
        job_name = f"nf-{spec.run_id.lower()[:50]}"

        service_account = opts.get("service_account")
        if not isinstance(service_account, str):
            service_account = None
        node_selector = _coerce_str_dict(opts.get("node_selector")) or None
        resources = opts.get("resources") if isinstance(opts.get("resources"), dict) else None

        manifest = _build_job_manifest(
            job_name=job_name,
            namespace=namespace,
            image=image,
            command=spec.command,
            env=env,
            in_pod_capsule_dir=in_pod_capsule,
            service_account=service_account,
            node_selector=node_selector,
            resources=resources,
        )

        timeout = spec.timeout_s if spec.timeout_s is not None else 600.0
        poll_interval = opts.get("poll_interval_s", 2.0)
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            poll_interval = 2.0

        # 1. Apply the Job manifest via stdin (no temp file, no race).
        try:
            apply = subprocess.run(
                [self._kubectl, "apply", "-f", "-"],
                input=json.dumps(manifest).encode(),
                capture_output=True, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                stderr=(exc.stderr or b""),
                runner_error=f"`kubectl apply` timed out: {exc}",
                runner_metadata={"image": image, "namespace": namespace, "job_name": job_name},
            )
        except OSError as exc:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup",
                runner_error=str(exc),
                runner_metadata={"image": image, "namespace": namespace, "job_name": job_name},
            )

        if apply.returncode != 0:
            return RunnerJobResult(
                exit_code=125, runner_status="failed_setup",
                stdout=apply.stdout, stderr=apply.stderr,
                runner_error="kubectl apply failed; see stderr",
                runner_metadata={"image": image, "namespace": namespace, "job_name": job_name},
            )

        # 2. Poll Job status until succeeded/failed or timeout.
        deadline = time.monotonic() + timeout
        pod_name: str | None = None
        succeeded = False
        failed = False
        while time.monotonic() < deadline:
            try:
                status = subprocess.run(
                    [self._kubectl, "get", "job", job_name,
                     "-n", namespace, "-o", "json"],
                    capture_output=True, timeout=10,
                )
            except (subprocess.TimeoutExpired, OSError):
                break
            if status.returncode != 0:
                break
            try:
                doc = json.loads(status.stdout.decode())
            except json.JSONDecodeError:
                break
            jstatus = doc.get("status", {})
            if jstatus.get("succeeded", 0) >= 1:
                succeeded = True
                break
            if jstatus.get("failed", 0) >= 1:
                failed = True
                break
            jittered_sleep(poll_interval)

        # 3. Find the pod (for log retrieval and kubectl cp).
        try:
            pods = subprocess.run(
                [self._kubectl, "get", "pods",
                 "-n", namespace,
                 "-l", f"job-name={job_name}",
                 "-o", "jsonpath={.items[0].metadata.name}"],
                capture_output=True, timeout=10,
            )
            if pods.returncode == 0 and pods.stdout.strip():
                pod_name = pods.stdout.decode().strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

        # 4. Pull stdout/stderr via kubectl logs.
        stdout_bytes = b""
        stderr_bytes = b""
        if pod_name:
            try:
                logs = subprocess.run(
                    [self._kubectl, "logs", pod_name, "-n", namespace],
                    capture_output=True, timeout=30,
                )
                stdout_bytes = logs.stdout
                stderr_bytes = logs.stderr
            except (subprocess.TimeoutExpired, OSError):
                pass

            # 5. Sync the in-pod capsule dir back to local FS.
            try:
                subprocess.run(
                    [self._kubectl, "cp",
                     f"{namespace}/{pod_name}:{in_pod_capsule}/.",
                     str(spec.capsule_dir)],
                    capture_output=True, timeout=120,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                stderr_bytes += f"[novafabric] kubectl cp failed: {exc}".encode()

        # 6. Best-effort cleanup of the Job (and its Pod).
        try:
            subprocess.run(
                [self._kubectl, "delete", "job", job_name,
                 "-n", namespace, "--wait=false"],
                capture_output=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

        runner_metadata: dict[str, Any] = {
            "image": image, "namespace": namespace, "job_name": job_name,
        }
        if pod_name:
            runner_metadata["pod_name"] = pod_name

        if not succeeded and not failed:
            return RunnerJobResult(
                exit_code=124, runner_status="timeout",
                stdout=stdout_bytes, stderr=stderr_bytes,
                runner_error=f"workload did not complete within {timeout}s",
                runner_metadata=runner_metadata,
            )

        # Job-level success/failure does not give us the workload exit
        # code directly (kubectl doesn't surface it cleanly without a
        # `kubectl get pod -o jsonpath=` ceremony). For the v0.6.1 first
        # cut, we report 0 on succeeded and 1 on failed; getting the
        # exact exit code is queued as a v0.6.x followup.
        exit_code = 0 if succeeded else 1
        return RunnerJobResult(
            exit_code=exit_code, runner_status="completed",
            stdout=stdout_bytes, stderr=stderr_bytes,
            runner_metadata=runner_metadata,
        )
