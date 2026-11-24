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


# ── invariant 4 says what the default level stores ──────────────────────────

_INVARIANT_4_TEXTS = [
    pytest.param(REPO_ROOT / "AGENTS.md", id="AGENTS.md"),
    pytest.param(REPO_ROOT / "docs" / "architecture.md", id="docs/architecture.md"),
]


def _invariant_4(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^4\. (.*?)(?=^5\. )", text, flags=re.S | re.M)
    assert match, f"{path.name}: numbered invariant 4 not found"
    return " ".join(match.group(1).split())


def _says_not_met(invariant: str) -> bool:
    """True only for the present-tense bold statement "invariant is **not met**".

    A bare substring would also accept "**satisfied** (it was once not met)" or "was once
    **not met** but is now opt-in", which say the opposite, so the statement must be the
    present-tense phrase and nothing may say it is satisfied.
    """
    stated = re.search(r"invariant is \*\*not met\*\*", invariant) is not None
    return stated and "satisfied" not in invariant


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("As built this invariant is **not met**: model calls are stored.", True),
        ("As built, this invariant is **not met**: model calls are stored.", True),
        ("This invariant was once **not met** but capture is now opt-in.", False),
        ("This invariant is **satisfied** (it was once not met).", False),
        ("It was once not met and is now **satisfied**; the **not met** note is gone.", False),
        ("Capture is opt-in.", False),
        ("", False),
    ],
    ids=[
        "unmet",
        "unmet-with-comma",
        "was-once-not-met",
        "satisfied-mentioning-not-met",
        "both-words",
        "no-statement",
        "empty",
    ],
)
def test_says_not_met_reads_the_statement_not_the_substring(text: str, expected: bool) -> None:
    assert _says_not_met(text) is expected


@pytest.mark.parametrize("path", _INVARIANT_4_TEXTS)
def test_invariant_4_matches_what_the_default_level_stores(path: Path) -> None:
    """The privacy invariant may only be stated as met when the default stores no content.

    ``LEVEL_EFFECTS[STANDARD]`` is the code-side statement of what the default level does.
    While it says model calls still store full prompts and responses, every public text of
    invariant 4 must say the invariant is not met; once it stops saying so, none may.
    """
    from novafabric.policies.capture_level import LEVEL_EFFECTS, CaptureLevel

    stores_content = "full prompts and responses" in LEVEL_EFFECTS[CaptureLevel.STANDARD]
    invariant = _invariant_4(path)
    says_not_met = _says_not_met(invariant)
    assert says_not_met == stores_content, (
        f"{path.name} invariant 4 and LEVEL_EFFECTS[standard] disagree "
        f"(default stores content: {stores_content}; text says not met: {says_not_met}). "
        "Update AGENTS.md and docs/architecture.md invariant 4 in the same change."
    )
    if stores_content:
        missing = [p for p in _WHAT_THE_DEFAULT_STORES if p not in invariant]
        assert not missing, (
            f"{path.name} invariant 4 says 'not met' but no longer says what is stored "
            f"(missing {missing}); the claim must name the file, the records and the levels."
        )


#: What a truthful "not met" must say: which file, which records, at which levels.
_WHAT_THE_DEFAULT_STORES = (
    "model-calls.jsonl",
    "request messages",
    "every capture level",
    "NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off",
)


def _level_comments() -> dict[str, str]:
    """Each ``CaptureLevel`` member's trailing comment, read from the source."""
    import inspect

    from novafabric.policies.capture_level import CaptureLevel

    source = inspect.getsource(CaptureLevel)
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r'^\s+([A-Z_]+) = "[a-z_]+"\s*# (.*)$', source, flags=re.M)
    }


def test_level_comments_say_what_each_level_does_today() -> None:
    """The member comments are a third place that states what a level does; pin them to the code.

    ``Today:`` is behaviour that exists, ``Planned (unscheduled):`` is not built. The facts each
    comment must carry come from ``PAYLOAD_LEVELS`` and ``LEVEL_EFFECTS``, not from this test.
    """
    from novafabric.policies.capture_level import PAYLOAD_LEVELS, CaptureLevel

    comments = _level_comments()
    assert set(comments) == {level.name for level in CaptureLevel}, comments
    for name, text in comments.items():
        level = CaptureLevel[name]
        today, _, planned = text.partition("Planned (unscheduled):")
        assert today.startswith("Today: "), f"{name}: comment must start with 'Today:': {text!r}"
        assert "Planned:" not in text, f"{name}: use 'Planned (unscheduled):': {text!r}"
        # Only the payload levels change capture; the comment says so exactly for them.
        assert ("tool payloads" in today) == (name == "FORENSIC"), f"{name}: {text!r}"
        assert ("same as" in today) == (name in {"MINIMAL", "AIR_GAPPED"}), f"{name}: {text!r}"
        # Whatever a level is said to be "the same as" must really behave the same.
        if "same as STANDARD" in today:
            assert level not in PAYLOAD_LEVELS, f"{name} changes capture; {text!r}"
        if "same as FORENSIC" in today:
            assert level in PAYLOAD_LEVELS, f"{name} does not change capture; {text!r}"
        # Planned wording appears exactly where the code says the feature is not applied.
        assert bool(planned.strip()) == (name in {"MINIMAL", "AIR_GAPPED"}), f"{name}: {text!r}"
