"""M4 — every ``mode: dashboard`` configuration the chart renders must actually start.

The chart passed ``--host 0.0.0.0`` (a pod must bind beyond loopback for its
Service and the kubelet's probes to reach it) and appended ``--insecure`` only
with ``serve.insecure``. Since ADR-0230 D1 that flag defaults to ``false``, and
``nova serve`` refuses a non-loopback bind without ``--insecure`` (exit 2) — so
``helm install --set mode=dashboard`` rendered cleanly and then crash-looped.
Two self-consistent artifacts, nothing spanning them; this file spans them:

* the bind policy itself is not weakened (``nova serve`` still refuses);
* the chart refuses at render time — before the risk or the crash loop exists —
  any dashboard configuration that ``nova serve`` would refuse at start;
* every dashboard configuration the chart DOES render passes the real bind gate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.cli.serve import non_loopback_bind_refused

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "deploy" / "helm" / "novafabric"
ACK = "i-accept-serving-evidence-over-plain-http"

helm_required = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is not installed"
)


def _render(*overrides: str) -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", "t", str(CHART)]
    for override in overrides:
        cmd += ["--set", override]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _nova_args(manifest: str) -> list[str]:
    for doc in yaml.safe_load_all(manifest):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            containers: list[dict[str, Any]] = doc["spec"]["template"]["spec"]["containers"]
            (nova,) = [c for c in containers if c["name"] == "nova"]
            return [str(a) for a in nova["args"]]
    raise AssertionError("no Deployment rendered")


def _bind(args: list[str]) -> tuple[str, bool]:
    host = args[args.index("--host") + 1] if "--host" in args else "127.0.0.1"
    return host, "--insecure" in args


# --------------------------------------------------------------------------- #
# The CLI bind policy is unchanged (ADR-0230 / cli/serve.py) — it is the fixed
# point the chart has to satisfy, not the thing to relax.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_serve_still_refuses_a_non_loopback_bind_without_insecure(host: str) -> None:
    assert non_loopback_bind_refused(host, insecure=False) is True
    assert non_loopback_bind_refused(host, insecure=True) is False


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_serve_allows_loopback(host: str) -> None:
    assert non_loopback_bind_refused(host, insecure=False) is False


def test_serve_cli_exits_2_on_the_bind_the_chart_used_to_render() -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    result = CliRunner().invoke(
        app, ["serve", "--experimental", "--host", "0.0.0.0", "--no-browser"]
    )
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 2
    assert "Refusing to bind" in result.output


# --------------------------------------------------------------------------- #
# The chart
# --------------------------------------------------------------------------- #


@helm_required
def test_dashboard_without_the_acknowledgement_refuses_to_render() -> None:
    """It used to render, then crash-loop on ``Refusing to bind ... without --insecure``."""
    result = _render("mode=dashboard")
    assert result.returncode != 0, "mode=dashboard rendered a pod that cannot start"
    assert "Refusing to render" in result.stderr
    assert "crash-loop" in result.stderr
    # Actionable: names both ways forward and the exact acknowledgement string.
    assert "mode: server" in result.stderr
    assert ACK in result.stderr


@helm_required
@pytest.mark.parametrize(
    "overrides",
    [
        (),  # stock: mode=server
        ("mode=dashboard", "serve.insecure=true", f"serve.acknowledgeInsecureExposure={ACK}"),
        (
            "mode=dashboard",
            "serve.insecure=true",
            f"serve.acknowledgeInsecureExposure={ACK}",
            "serve.topology=false",
            "serve.tv5=false",
        ),
    ],
)
def test_every_rendered_serve_configuration_passes_the_real_bind_gate(
    overrides: tuple[str, ...],
) -> None:
    result = _render(*overrides)
    assert result.returncode == 0, result.stderr
    args = _nova_args(result.stdout)
    if args[0] != "serve":
        return  # `nova server start` has no loopback gate (it has OIDC/RBAC)
    host, insecure = _bind(args)
    assert not non_loopback_bind_refused(host, insecure=insecure), (
        f"chart rendered `nova {' '.join(args)}`, which nova serve refuses (exit 2)"
    )
