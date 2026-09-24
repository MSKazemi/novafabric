"""ADR-0230 — the deployed default is safe, and stays describable.

Two things are guarded here, and they are the same defect wearing two faces.

The chart shipped ``mode: dashboard`` + ``serve.insecure: true``: a dashboard
capable of irreversible evidence operations, bound beyond loopback over plain
HTTP, behind one shared token. That combination matched **none** of ADR-0042's
four named, tested and supported deployment tiers — it was an unsupported
configuration, not merely a risky one.

Next to it sat a comment calling the dashboard *"read-only"*, which it has not
been since v0.8. **The false claim was load-bearing for the unsafe default**: the
plausible reason the default survived review is that the line beside it said the
thing behind it was safe. So the phrase is guarded too — and it has now survived
two manual sweeps (ROADMAP's v0.98.1 pass, and ADR-0230 D4's own), which is the
argument for a guard rather than a third sweep.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# D1 + D2 — the default is safe
# ---------------------------------------------------------------------------


@helm_required
def test_stock_values_render() -> None:
    result = _render()
    assert result.returncode == 0, result.stderr


@helm_required
def test_stock_values_never_emit_insecure() -> None:
    """D1. The flag that disables the non-loopback bind refusal is not a default."""
    result = _render()
    assert result.returncode == 0, result.stderr
    assert "--insecure" not in result.stdout


@helm_required
def test_stock_values_run_the_multi_user_component() -> None:
    """D2. A chart is by definition a multi-user deployment."""
    result = _render()
    assert result.returncode == 0, result.stderr
    assert "- server" in result.stdout and "- start" in result.stdout
    # Exact arg lines: "- serve" is a prefix of "- server", so a substring test
    # here passes for the wrong reason and then fails for the right one.
    args = [line.strip() for line in result.stdout.splitlines()]
    assert "- server" in args
    assert "- serve" not in args


@helm_required
def test_dashboard_mode_is_still_supported_when_chosen() -> None:
    """The problem was the default, not the option."""
    result = _render("mode=dashboard")
    assert result.returncode == 0, result.stderr
    args = [line.strip() for line in result.stdout.splitlines()]
    assert "- serve" in args
    assert "--insecure" not in result.stdout


# ---------------------------------------------------------------------------
# D3 — the unsafe combination fails at render time
# ---------------------------------------------------------------------------


@helm_required
def test_insecure_without_acknowledgement_refuses_to_render() -> None:
    """A NOTES.txt warning prints after the exposure exists. This arrives before."""
    result = _render("mode=dashboard", "serve.insecure=true")
    assert result.returncode != 0, "insecure=true rendered without acknowledgement"
    assert "Refusing to render" in result.stderr


@helm_required
def test_the_refusal_names_the_risk_and_both_ways_forward() -> None:
    """A refusal an operator cannot act on gets worked around, not heeded."""
    stderr = _render("mode=dashboard", "serve.insecure=true").stderr
    assert "admin" in stderr and "irreversible" in stderr
    assert "ADR-0042" in stderr, "the unsupported-tier argument is the strongest one"
    assert "terminate TLS at the ingress" in stderr
    assert ACK in stderr, "the refusal must name the exact acknowledgement string"


@helm_required
def test_the_acknowledgement_lets_it_through() -> None:
    """Possible but effortful. Refusing outright would just fork the chart."""
    result = _render(
        "mode=dashboard", "serve.insecure=true", f"serve.acknowledgeInsecureExposure={ACK}"
    )
    assert result.returncode == 0, result.stderr
    assert "--insecure" in result.stdout


@helm_required
def test_a_wrong_acknowledgement_does_not_count() -> None:
    """`true`, `yes` and friends are exactly what a reflex sets."""
    for wrong in ("true", "yes", "1", "i-accept"):
        result = _render(
            "mode=dashboard", "serve.insecure=true", f"serve.acknowledgeInsecureExposure={wrong}"
        )
        assert result.returncode != 0, f"{wrong!r} was accepted as an acknowledgement"


# ---------------------------------------------------------------------------
# D4 — the phrase that made the unsafe default look safe
# ---------------------------------------------------------------------------

#: Whole-product claims only. ``read-only endpoint``, ``Read-only inspection``
#: and ``read-only (`/api/runs/{id}/energy`)`` are TRUE of the things they
#: describe and appear ~21 times; flagging them would train a reader to ignore
#: this guard, which is worse than not having it.
_FALSE_CLAIM = re.compile(
    r"read[-\s]only\s+(\w+\s+)?(dashboard|`?nova\s+serve`?)"
    r"|`?nova\s+serve`?\s+is\s+[^.\n]*read[-\s]only",
    re.IGNORECASE,
)

#: A dated release note is a historical record of what was said at the time.
#: Amending it rewrites the record, so it is excluded rather than fixed.
_EXCLUDED = ("docs/releases/",)


def _searchable_files() -> list[Path]:
    files: list[Path] = []
    for root in ("docs", "deploy"):
        for path in sorted((REPO / root).rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".yaml", ".yml", ".sh", ".txt"}:
                continue
            rel = path.relative_to(REPO).as_posix()
            if any(rel.startswith(prefix) for prefix in _EXCLUDED):
                continue
            files.append(path)
    files.append(REPO / "README.md")
    return files


def test_nothing_describes_nova_serve_as_read_only() -> None:
    """It has not been read-only since v0.8, and the claim guarded an unsafe default."""
    offenders: list[str] = []
    for path in _searchable_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _FALSE_CLAIM.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:110]}")
    assert not offenders, (
        "`nova serve` is described as read-only in "
        f"{len(offenders)} place(s) — it exposes DELETE /api/runs/{{id}}, "
        "POST /api/compliance/pii/erase, POST /api/seal/{id}/bypass and "
        "POST /api/admin/roles:\n" + "\n".join(offenders)
    )


def test_the_guard_matches_the_phrasings_it_exists_to_catch() -> None:
    """Non-vacuity. A guard that matches nothing passes for the wrong reason."""
    for claim in (
        "the experimental read-only dashboard",
        "a local read-only web dashboard",
        "the read-only `nova serve` dashboard",
        "Deploys the read-only nova serve dashboard",
        "nova serve is experimental/read-only; --insecure serves over HTTP",
    ):
        assert _FALSE_CLAIM.search(claim), f"guard missed a real phrasing: {claim!r}"


def test_the_guard_leaves_true_per_endpoint_uses_alone() -> None:
    """The other half of the same claim — a noisy guard is an ignored guard."""
    for legitimate in (
        "every read-only endpoint — runs, lineage, evidence, compliance reads",
        "Read-only inspection, no subprocess, safe.",
        "read-only (`/api/runs/{id}/{energy,ledger,safety-case}`)",
        "a read-only manifest listing",
        "Layer A (v0.7) is read-only",
    ):
        assert not _FALSE_CLAIM.search(legitimate), f"guard over-matched: {legitimate!r}"


def test_release_notes_are_excluded_from_the_sweep() -> None:
    """Explicit, because an exclusion nobody states reads as coverage."""
    assert not any(
        path.relative_to(REPO).as_posix().startswith("docs/releases/")
        for path in _searchable_files()
    )
    # And the exclusion is load-bearing: a release note really does carry the
    # old phrasing, so dropping it would make the sweep fail on history.
    historical = REPO / "docs" / "releases" / "v0.58.0.md"
    if historical.is_file():
        assert _FALSE_CLAIM.search(historical.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# D5 — the flip is not silent on upgrade
# ---------------------------------------------------------------------------


@helm_required
def test_an_upgrade_refuses_until_the_operator_states_their_intent() -> None:
    """Changing what a deployment runs without saying so is not acceptable.

    Even when the new behaviour is safer: an operator upgrading with a values
    file that pinned neither key would silently get a different component on a
    different port.
    """
    result = subprocess.run(
        ["helm", "template", "t", str(CHART), "--is-upgrade"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "the default flip upgraded silently"
    assert "defaults changed" in result.stderr


@helm_required
def test_the_upgrade_message_carries_the_values_that_restore_the_old_behaviour() -> None:
    """A breaking-change message without the fix is a complaint, not a migration."""
    stderr = subprocess.run(
        ["helm", "template", "t", str(CHART), "--is-upgrade"],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    assert "mode: dashboard" in stderr
    assert "insecure: true" in stderr
    assert ACK in stderr
    assert "upgradeAcknowledged: true" in stderr


@helm_required
def test_acknowledging_the_upgrade_lets_it_proceed() -> None:
    result = subprocess.run(
        ["helm", "template", "t", str(CHART), "--is-upgrade", "--set", "upgradeAcknowledged=true"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@helm_required
def test_a_fresh_install_never_sees_the_upgrade_gate() -> None:
    """A fresh install has no previous behaviour to lose."""
    assert _render().returncode == 0
