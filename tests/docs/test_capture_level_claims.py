"""The capture-level docs and ``--help`` claim only what the code does.

Regression: ``docs/cli-reference.md`` and the feature tour said ``minimal``
captured "run metadata only; no model IDs" and ``standard`` "redacted PII",
that ``set`` set a policy, and that "by default NovaFabric does not capture
full prompts". None of it held: ``CaptureLevelPolicy.validate_event`` had no
caller, ``set`` persisted nothing, and a capture of ``examples/blackbox_demo``
at ``minimal``, ``standard`` and ``forensic`` wrote identical
``model-calls.jsonl`` with full prompt text.

These tests pin the facts the corrected docs rest on. When one of them goes
red because the feature landed, update the docs in the same change.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "novafabric"
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"
FEATURE_TOUR = REPO_ROOT / "docs" / "tutorials" / "feature-tour.md"

runner = CliRunner()

_UPDATE_DOCS = (
    "The capture-level docs describe this as not implemented. If it now is, update "
    "docs/cli-reference.md (nova policy capture-level), docs/tutorials/feature-tour.md §12 "
    "and the `nova policy capture-level` --help text in the same change."
)


def _section(text: str, heading: str, next_heading_prefix: str = "\n## ") -> str:
    start = text.index(heading)
    end = text.find(next_heading_prefix, start + len(heading))
    return text[start : end if end != -1 else len(text)]


# ── the code facts ──────────────────────────────────────────────────────────


def test_capture_level_policy_validate_event_has_no_caller() -> None:
    """The per-field allowlist is applied nowhere: nothing calls ``.validate_event(``."""
    callers = [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in sorted(SRC.rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\.validate_event\(", line)
    ]
    assert callers == [], f"{callers}\n{_UPDATE_DOCS}"


@pytest.mark.parametrize("level", ["minimal", "standard"])
def test_minimal_and_standard_do_not_differ(level: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.capture.record import _payloads_enabled

    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", level)
    assert _payloads_enabled() is False, _UPDATE_DOCS


@pytest.mark.parametrize("level", ["forensic", "air_gapped"])
def test_forensic_levels_enable_payloads(level: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one thing a level does change today — the docs say so, keep it true."""
    from novafabric.capture.record import _payloads_enabled

    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", level)
    assert _payloads_enabled() is True


def test_set_persists_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_CAPTURE_LEVEL", raising=False)
    set_result = runner.invoke(app, ["policy", "capture-level", "set", "--level", "forensic"])
    assert set_result.exit_code == 0, set_result.output
    assert "Nothing was saved" in set_result.output
    assert "export NOVA_CAPTURE_LEVEL=forensic" in set_result.output
    get_result = runner.invoke(app, ["policy", "capture-level", "get"])
    assert get_result.exit_code == 0, get_result.output
    assert "Current capture level: standard" in get_result.output


def test_set_rejects_an_unknown_level() -> None:
    result = runner.invoke(app, ["policy", "capture-level", "set", "--level", "full"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)


# ── the --help text ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("sub", ["get", "set"])
def test_help_is_labelled_experimental_and_names_real_levels(sub: str) -> None:
    result = runner.invoke(app, ["policy", "capture-level", sub, "--help"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "experimental" in out
    # The old help listed levels that do not exist and examples that exit 1.
    for bogus in ("metadata-only", "events-only", "set full"):
        assert bogus not in out
    assert "policy for new runs" not in out


def test_set_help_says_it_persists_nothing() -> None:
    result = runner.invoke(app, ["policy", "capture-level", "set", "--help"])
    assert "Persists nothing" in " ".join(result.output.split())


# ── the docs ────────────────────────────────────────────────────────────────


def test_cli_reference_capture_level_section_is_honest() -> None:
    section = _section(
        CLI_REFERENCE.read_text(encoding="utf-8"), "### nova policy capture-level get", "\n---"
    )
    assert "**Status: experimental**" in section
    assert "persists nothing" in section
    assert "**Planned:**" in section
    assert "model-calls.jsonl" in section
    # The over-claims this section used to make.
    assert "Run metadata only; no model IDs" not in section
    assert "Print instructions for setting a new capture level" not in section
    assert "no external network calls permitted" not in section


def test_feature_tour_does_not_claim_prompts_are_opt_in() -> None:
    section = _section(FEATURE_TOUR.read_text(encoding="utf-8"), "## 12. Manage capture-level policy")
    assert "does not capture full prompts" not in section
    assert "records different field sets depending on the capture-level policy" not in section
    assert "No level keeps prompts out of a capsule" in section
