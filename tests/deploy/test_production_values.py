"""Issue #223 — ``values-production.yaml`` is secure by default and stays that way.

The stock ``values.yaml`` is an evaluation default: it bundles a single-replica
Postgres whose password is the literal ``nova``. ``values-production.yaml`` is
the layered production starting point. Two layers guard it:

* YAML-level tests below run everywhere (CI has no ``helm`` in the unit shards):
  they pin the hardening keys, and that every key the file sets exists in the
  stock ``values.yaml`` — a misspelt key is silently ignored by Helm, which would
  leave the insecure default in force while the file *looks* hardened.
* ``helm template`` tests run where ``helm`` is installed. The
  ``release-toolchain`` workflow renders the same file with real Helm on every
  pull request that touches ``deploy/helm/**``; the last test here keeps that
  step from being dropped.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "deploy" / "helm" / "novafabric"
PROD = CHART / "values-production.yaml"
STOCK = CHART / "values.yaml"
TOOLCHAIN = REPO / ".github" / "workflows" / "release-toolchain.yml"

#: Keys whose children are Kubernetes objects passed through ``toYaml``; the
#: stock values.yaml cannot list every field Kubernetes accepts under them.
FREEFORM = {"podSecurityContext", "securityContext", "resources"}

REQUIRED = (
    "--set",
    "externalDatabase.host=pg.example.invalid",
    "--set",
    "externalDatabase.existingSecret=nova-db",
)

helm_required = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} is not a YAML mapping"
    return data


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return _load(PROD)


# ---------------------------------------------------------------------------
# YAML level — runs without helm
# ---------------------------------------------------------------------------


def test_runs_the_authenticated_server_not_the_dashboard(prod: dict[str, Any]) -> None:
    assert prod["mode"] == "server"
    assert prod["serve"]["insecure"] is False
    assert not prod["serve"]["acknowledgeInsecureExposure"]


def test_no_bundled_evaluation_postgres(prod: dict[str, Any]) -> None:
    assert prod["postgres"]["enabled"] is False


def test_database_password_comes_only_from_an_existing_secret(prod: dict[str, Any]) -> None:
    db = prod["externalDatabase"]
    assert db["requireExistingSecret"] is True
    assert db["password"] == "", "a password in values lands in Helm's release record"
    # Left empty on purpose: the operator must name their own Secret, and the
    # chart refuses to render until they do.
    assert db["existingSecret"] == ""
    assert db["host"] == ""


def test_container_is_non_root_with_a_read_only_root_filesystem(prod: dict[str, Any]) -> None:
    ctx = prod["securityContext"]
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["allowPrivilegeEscalation"] is False
    assert ctx["runAsNonRoot"] is True
    assert ctx["privileged"] is False
    assert ctx["capabilities"]["drop"] == ["ALL"]
    assert "add" not in ctx["capabilities"]
    assert ctx["seccompProfile"]["type"] == "RuntimeDefault"


def test_pod_is_non_root_and_matches_the_image_uid(prod: dict[str, Any]) -> None:
    """uid/gid 1000 is the ``nova`` user deploy/docker/Dockerfile creates."""
    pod = prod["podSecurityContext"]
    assert pod["runAsNonRoot"] is True
    assert pod["runAsUser"] == 1000
    assert pod["runAsGroup"] == 1000
    assert pod["fsGroup"] == 1000
    assert pod["seccompProfile"]["type"] == "RuntimeDefault"


def test_writable_tmp_is_bounded(prod: dict[str, Any]) -> None:
    assert prod["tmpVolume"]["sizeLimit"], "an unbounded /tmp can fill the node's disk"


def test_resources_have_requests_and_a_memory_limit(prod: dict[str, Any]) -> None:
    res = prod["resources"]
    assert res["requests"]["cpu"] and res["requests"]["memory"]
    assert res["limits"]["memory"]


def test_persistence_is_on(prod: dict[str, Any]) -> None:
    assert prod["persistence"]["enabled"] is True


def test_single_writer(prod: dict[str, Any]) -> None:
    assert prod["replicaCount"] == 1


def test_ingress_is_left_to_the_operator(prod: dict[str, Any]) -> None:
    """No default host or TLS policy is invented for someone else's cluster."""
    assert prod["ingress"]["enabled"] is False


@pytest.mark.parametrize("var", ["NOVAFABRIC_AUDIT_LOG_PATH", "NOVAFABRIC_KEYRING_DIR"])
def test_home_relative_state_lives_on_the_data_volume(prod: dict[str, Any], var: str) -> None:
    """With a read-only root filesystem the ~/.local/share and ~/.config defaults are unwritable."""
    env = {e["name"]: e.get("value") for e in prod["extraEnv"]}
    path = env.get(var)
    assert path, f"{var} is not set"
    assert path.startswith("/data/"), f"{var}={path} is not on the /data volume"


def _paths(node: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if not isinstance(node, dict):
        return []
    out: list[tuple[str, ...]] = []
    for key, value in node.items():
        path = (*prefix, str(key))
        out.append(path)
        if key not in FREEFORM:
            out.extend(_paths(value, path))
    return out


def _has(node: Any, path: tuple[str, ...]) -> bool:
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def test_every_key_exists_in_the_stock_values(prod: dict[str, Any]) -> None:
    """Helm ignores an unknown key, so a typo would silently keep the insecure default."""
    stock = _load(STOCK)
    unknown = [".".join(p) for p in _paths(prod) if not _has(stock, p)]
    assert not unknown, (
        f"keys absent from values.yaml (typo, or a value the chart ignores): {unknown}"
    )


def test_stock_values_keep_the_new_keys_off() -> None:
    """The stock defaults are unchanged by this file's existence."""
    stock = _load(STOCK)
    assert stock["securityContext"]["readOnlyRootFilesystem"] is False
    assert stock["externalDatabase"]["requireExistingSecret"] is False
    assert stock["extraVolumes"] == []
    assert stock["extraVolumeMounts"] == []


def test_templates_wire_the_guard_and_the_tmp_volume() -> None:
    deployment = (CHART / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    helpers = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert '{{- include "novafabric.assertExistingSecret" . -}}' in deployment
    assert '{{- define "novafabric.assertExistingSecret" -}}' in helpers
    assert helpers.count("Refusing to render: externalDatabase.requireExistingSecret=true") == 3
    # /tmp is mounted in BOTH the migration init container and the app container.
    assert deployment.count("mountPath: /tmp") == 2


def test_release_toolchain_renders_the_production_values() -> None:
    workflow = yaml.safe_load(TOOLCHAIN.read_text(encoding="utf-8"))
    steps = [s for job in workflow["jobs"].values() for s in job.get("steps", [])]
    render = [s for s in steps if s.get("name") == "Render production values"]
    assert render, "release-toolchain.yml lost the production-values render step"
    script = render[0]["run"]
    assert "values-production.yaml" in script
    assert "helm lint" in script and "helm template" in script
    assert "postgres.enabled=true" in script
    assert "externalDatabase.password=" in script


# ---------------------------------------------------------------------------
# helm template — runs where helm is installed
# ---------------------------------------------------------------------------


def _render(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", "t", str(CHART), "-f", str(PROD), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _docs(stdout: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(stdout) if isinstance(d, dict)]


@helm_required
def test_renders_with_the_two_required_fields() -> None:
    result = _render(*REQUIRED)
    assert result.returncode == 0, result.stderr


@helm_required
def test_rendered_pod_is_hardened() -> None:
    result = _render(*REQUIRED)
    assert result.returncode == 0, result.stderr
    docs = _docs(result.stdout)
    kinds = [d["kind"] for d in docs]
    assert "Secret" not in kinds, "no chart-managed Secret in production"
    (app,) = [
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") != "postgres"
    ]
    assert not [
        d
        for d in docs
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "postgres"
    ], "the bundled Postgres rendered"
    spec = app["spec"]["template"]["spec"]
    for container in spec["containers"] + spec["initContainers"]:
        ctx = container["securityContext"]
        assert ctx["readOnlyRootFilesystem"] is True
        assert ctx["allowPrivilegeEscalation"] is False
        mounts = {m["mountPath"] for m in container.get("volumeMounts", [])}
        assert "/tmp" in mounts, f"{container['name']} has no writable /tmp"
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["tmp"]["emptyDir"]["sizeLimit"] == "1Gi"
    env = {e["name"]: e for e in spec["containers"][0]["env"]}
    assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "nova-db"


@helm_required
def test_refuses_without_an_existing_secret() -> None:
    result = _render("--set", "externalDatabase.host=pg.example.invalid")
    assert result.returncode != 0
    assert "existingSecret is empty" in result.stderr


@helm_required
def test_refuses_the_bundled_postgres() -> None:
    result = _render(*REQUIRED, "--set", "postgres.enabled=true")
    assert result.returncode != 0
    assert "postgres.enabled=true" in result.stderr


@helm_required
def test_refuses_a_password_in_values() -> None:
    result = _render(*REQUIRED, "--set", "externalDatabase.password=hunter2")
    assert result.returncode != 0
    assert "externalDatabase.password is set" in result.stderr


@helm_required
def test_stock_values_render_no_tmp_volume() -> None:
    result = subprocess.run(
        ["helm", "template", "t", str(CHART)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    (app,) = [
        d
        for d in _docs(result.stdout)
        if d["kind"] == "Deployment"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") != "postgres"
    ]
    names = {v["name"] for v in app["spec"]["template"]["spec"]["volumes"]}
    assert "tmp" not in names
