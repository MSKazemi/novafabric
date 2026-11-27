"""The chart's migration init container migrates the track the server checks.

``nova server start`` refuses to start unless the registry track's Alembic stamp
in ``NOVAFABRIC_POSTGRES_DSN`` is at head (``novafabric.server.schema_skew``). The
init container ran ``nova db upgrade --backend postgres`` — the MetadataStore
track, ``nova db upgrade``'s default — against that same database. Both tracks
keep their stamp in the one ``alembic_version`` table, so the database ended up
stamped ``v005`` (MetadataStore head), which the registry track does not know,
and the stock chart crash-looped with ``SchemaSkewError``. Found by a Kind
install smoke; the Ansible role hit the same thing.

Server mode now runs ``--track registry`` only. The MetadataStore tables the
server uses are created by ``PostgresMetadataStore.bootstrap()`` on first use
(``server/deps.py``), so the metadata track is not needed for the server to
start. Dashboard mode (``nova serve``, SQLite registry) is unchanged.
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
ACK = "i-accept-serving-evidence-over-plain-http"

helm_required = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def test_template_runs_the_registry_track_in_server_mode() -> None:
    text = (CHART / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    server_branch = text.split('{{- if eq .Values.mode "server" }}', 1)[1].split("{{- else }}")[0]
    assert "nova db upgrade --track registry --backend postgres" in server_branch


def test_server_deps_still_bootstrap_the_metadata_store() -> None:
    """The fix relies on the server creating its MetadataStore tables itself."""
    deps = (REPO / "src" / "novafabric" / "server" / "deps.py").read_text(encoding="utf-8")
    assert "store.bootstrap()" in deps, (
        "server/deps.py no longer bootstraps the MetadataStore; the chart's init "
        "container migrates only the registry track, so nothing would create those tables"
    )


def _migrate_script(*overrides: str) -> str:
    cmd = ["helm", "template", "t", str(CHART)]
    for override in overrides:
        cmd += ["--set", override]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    docs: list[dict[str, Any]] = [
        d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)
    ]
    (app,) = [
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") != "postgres"
    ]
    (init,) = app["spec"]["template"]["spec"]["initContainers"]
    return str(init["command"][-1])


def _upgrade_lines(script: str) -> list[str]:
    return [line.strip() for line in script.splitlines() if "nova db upgrade" in line]


@helm_required
def test_stock_server_mode_migrates_only_the_registry_track() -> None:
    lines = _upgrade_lines(_migrate_script())
    assert lines == ['nova db upgrade --track registry --backend postgres --revision "head"'], lines


@helm_required
def test_db_revision_is_passed_to_the_registry_track() -> None:
    lines = _upgrade_lines(_migrate_script("dbRevision=p0002"))
    assert lines == ['nova db upgrade --track registry --backend postgres --revision "p0002"']


@helm_required
def test_dashboard_mode_migration_is_unchanged() -> None:
    lines = _upgrade_lines(
        _migrate_script(
            "mode=dashboard", "serve.insecure=true", f"serve.acknowledgeInsecureExposure={ACK}"
        )
    )
    assert lines == ['nova db upgrade --backend postgres --revision "head"'], lines
