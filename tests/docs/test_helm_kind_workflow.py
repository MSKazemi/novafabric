"""The Kind chart smoke workflow keeps the shape issue #224 asks for.

``.github/workflows/helm-kind.yml`` is the only job that installs the Helm chart into a
cluster; ``release-toolchain.yml`` only lints, renders and packages it. A job like this
decays quietly: a path filter that stops matching the chart, a step that stops running the
upgrade, or a diagnostics dump that starts printing a Secret all still produce a green
check. These assertions read the workflow and ``scripts/helm_kind_smoke.sh`` as text and
YAML; they never start a cluster.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "helm-kind.yml"
SCRIPT = REPO / "scripts" / "helm_kind_smoke.sh"

_PINNED_USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s*v\d[\w.-]*\s*$"
)


def _doc() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers() -> dict[str, Any]:
    doc = _doc()
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True)) or {}


def _job() -> dict[str, Any]:
    jobs = _doc()["jobs"]
    assert list(jobs) == ["helm-smoke"], f"expected one job `helm-smoke`, found {list(jobs)}"
    return jobs["helm-smoke"]


def _steps() -> list[dict[str, Any]]:
    return [s for s in _job()["steps"] if isinstance(s, dict)]


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_files_exist() -> None:
    assert WORKFLOW.is_file()
    assert SCRIPT.is_file()


@pytest.mark.parametrize(
    "path",
    [
        "deploy/helm/**",
        "src/novafabric/server/**",
        "deploy/docker/Dockerfile",
        ".github/workflows/helm-kind.yml",
        "scripts/helm_kind_smoke.sh",
    ],
)
def test_runs_on_pull_requests_touching_the_chart_server_image_or_itself(path: str) -> None:
    assert path in _triggers()["pull_request"]["paths"]


def test_also_runs_nightly_and_on_demand() -> None:
    triggers = _triggers()
    assert triggers.get("schedule"), "no nightly schedule"
    assert "workflow_dispatch" in triggers


def test_least_privilege_and_bounded() -> None:
    assert _doc()["permissions"] == {"contents": "read"}
    job = _job()
    assert "permissions" not in job, "the job must not widen the workflow's permissions"
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < int(job["timeout-minutes"]) <= 60
    assert job["if"] == "github.repository == 'MSKazemi/novafabric'"


def test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment() -> None:
    lines = [ln for ln in WORKFLOW.read_text(encoding="utf-8").splitlines() if "uses:" in ln]
    assert lines, "no actions found"
    unpinned = [ln.strip() for ln in lines if not _PINNED_USES.match(ln)]
    assert not unpinned, f"actions not pinned to a 40-hex SHA with a `# vX` comment: {unpinned}"


def test_image_is_built_from_the_tree_and_never_pushed() -> None:
    builds = [s for s in _steps() if str(s.get("uses", "")).startswith("docker/build-push-action@")]
    assert len(builds) == 1
    with_ = builds[0]["with"]
    assert with_["file"] == "deploy/docker/Dockerfile"
    assert with_["context"] == "."
    assert with_["push"] is False
    assert with_["load"] is True


def test_helm_binary_is_checksum_verified() -> None:
    env = _job()["env"]
    assert re.fullmatch(r"[0-9a-f]{64}", env["HELM_SHA256"])
    install = next(s for s in _steps() if "get.helm.sh" in str(s.get("run", "")))
    assert "sha256sum -c" in install["run"]


def test_image_is_loaded_into_kind_and_never_pulled() -> None:
    runs = "\n".join(str(s.get("run", "")) for s in _steps())
    assert "kind load docker-image" in runs
    assert "pullPolicy: Never" in _script()


def test_the_lifecycle_runs_through_the_script() -> None:
    runs = [str(s.get("run", "")) for s in _steps()]
    assert any("scripts/helm_kind_smoke.sh run" in r for r in runs)


def test_failure_diagnostics_are_collected_and_uploaded() -> None:
    steps = _steps()
    collect = [s for s in steps if "helm_kind_smoke.sh diagnostics" in str(s.get("run", ""))]
    upload = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(collect) == 1 and len(upload) == 1
    for step in (collect[0], upload[0]):
        assert step.get("if") in ("failure()", "always()"), step
    assert steps.index(collect[0]) < steps.index(upload[0])
    # The state directory holds the generated secrets; only the diagnostics dir is uploaded.
    assert "helm-kind-diagnostics" in upload[0]["with"]["path"]
    assert "helm-kind-smoke" not in upload[0]["with"]["path"]
    assert int(upload[0]["with"]["retention-days"]) <= 14


def test_script_is_strict_bash() -> None:
    text = _script()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text
    assert not re.search(r"^\s*set\s+-[a-z]*x", text, re.MULTILINE), "set -x would trace secrets"


def test_script_exercises_install_smoke_upgrade_and_rollback() -> None:
    text = _script()
    for needle in ("helm install", "helm upgrade", "helm rollback", "port-forward"):
        assert needle in text, needle
    # Authenticated call, the same call without a token, and with a wrong one.
    assert re.search(r'expect_status "\$phase" "authenticated API call" "200" /v0/version', text)
    assert re.search(r'expect_status "\$phase" "no token" "401\|403" /v0/version\n', text)
    assert re.search(r'expect_status "\$phase" "wrong token" "401\|403" /v0/version', text)
    assert '"200" /livez' in text and '"200" /readyz' in text
    # Smoked after each of the three lifecycle steps.
    for phase in ("install", "upgrade", "rollback"):
        assert re.search(rf"^\s*smoke {phase}$", text, re.MULTILINE), phase


def test_script_uses_real_chart_keys_and_the_existing_secret() -> None:
    text = _script()
    values = (REPO / "deploy" / "helm" / "novafabric" / "values.yaml").read_text(encoding="utf-8")
    chart = yaml.safe_load(values)
    assert "existingSecret" in chart["externalDatabase"]
    assert "upgradeAcknowledged" in chart
    assert "extraEnv" in chart
    assert "existingSecret: ${DB_SECRET}" in text
    assert "postgres:\n  enabled: false" in text
    # The server pins its local bearer token from this variable.
    token_module = (REPO / "src" / "novafabric" / "server" / "local_token.py").read_text()
    assert 'TOKEN_ENV: Final[str] = "NOVAFABRIC_SERVER_TOKEN"' in token_module
    assert "name: NOVAFABRIC_SERVER_TOKEN" in text


def test_script_never_puts_a_secret_value_on_a_command_line_or_in_output() -> None:
    text = _script()
    # Secrets are created from files, not literals (a literal sits in the process argv).
    assert "--from-literal" not in text
    assert text.count("--from-file=") >= 2
    # curl reads the Authorization header from a file.
    assert '-H "@$header_file"' in text
    assert "Bearer $" not in text.replace("Bearer %s", "")
    # Secret objects are listed by name only, never dumped.
    for line in text.splitlines():
        if re.search(r"\bget\s+secrets?\b", line):
            assert not re.search(r"-o\s*(yaml|json|jsonpath|go-template)", line), line
    assert "describe secret" not in text
    # Every collected file is checked for the generated values.
    assert "grep -rlF -f" in text
    assert "assert_no_secret_in" in text
