"""``nova diff name@version name@version`` honours ``--output-format`` (issue #11 follow-up).

The asset-ref path ignored the flag and always printed Rich text, so a CI step
asking for ``json`` got a document it could not parse and one asking for
``github-annotation`` got no annotations. It now emits the field-level document
``nova asset diff --output-format json`` already defines (plus ``has_changes``,
ADR-0303), or workflow-command annotations, and the gate is unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

import novafabric.cli.diff as diff_cli
from novafabric.cli.main import app

runner = CliRunner()

SPECS: dict[str, dict[str, Any]] = {
    "1": {"model": {"name": "gpt-4o", "temperature": 0.1}, "tools": ["a"], "old": None},
    "2": {"model": {"name": "gpt-4o", "temperature": 0.9}, "tools": ["a"], "new": "x"},
    "3": {"note\n::error::forged": "[bold] 100%"},
    "4": {"note": "plain"},
}


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diff_cli,
        "get_asset",
        lambda name, version: {"spec_json": json.dumps(SPECS[version])},
    )


def _run(*args: str) -> Any:
    return runner.invoke(app, ["diff", *args])


def test_json_is_the_asset_diff_document() -> None:
    result = _run("agent@1", "agent@2", "--output-format", "json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc == {
        "ref_a": "agent@1",
        "ref_b": "agent@2",
        "has_changes": True,
        "identical": False,
        "added": {"new": "x"},
        "removed": {"old": None},  # present-as-null on A, absent on B: removed
        "changed": {"model.temperature": {"from": 0.1, "to": 0.9}},
    }


def test_json_has_the_same_keys_as_nova_asset_diff_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novafabric.cli.asset as asset_cli

    monkeypatch.setattr(asset_cli, "get_asset", diff_cli.get_asset)
    ours = json.loads(_run("agent@1", "agent@2", "--output-format", "json").stdout)
    theirs = json.loads(
        runner.invoke(app, ["asset", "diff", "agent@1", "agent@2", "--output-format", "json"]).stdout
    )
    assert set(ours) - {"has_changes"} == set(theirs)
    for key in ("added", "removed", "changed", "identical"):
        assert ours[key] == theirs[key], key


def test_json_identical_specs() -> None:
    doc = json.loads(_run("agent@1", "agent@1", "--output-format", "json").stdout)
    assert doc["has_changes"] is False and doc["identical"] is True
    assert doc["added"] == doc["removed"] == doc["changed"] == {}


def test_github_annotations_are_errors_on_a_difference() -> None:
    lines = _run("agent@1", "agent@2", "--output-format", "github-annotation").stdout.splitlines()
    assert lines == [
        "::error title=NovaFabric Diff::Asset spec field new: (absent) → 'x'",
        "::error title=NovaFabric Diff::Asset spec field old: None → (absent)",
        "::error title=NovaFabric Diff::Asset spec field model.temperature: 0.1 → 0.9",
    ]


def test_github_annotations_notice_when_identical() -> None:
    out = _run("agent@1", "agent@1", "--output-format", "github-annotation").stdout
    assert out.splitlines() == ["::notice title=NovaFabric Diff::No differences found."]


def test_annotation_values_cannot_inject_a_workflow_command() -> None:
    """A newline in a registered spec key must not start a second workflow command."""
    out = _run("agent@3", "agent@4", "--output-format", "github-annotation").stdout
    lines = out.splitlines()
    assert not any(line.startswith("::error::forged") for line in lines), lines
    assert all(line.startswith("::error title=NovaFabric Diff::") for line in lines), lines
    assert any("note%0A::error::forged" in line and "100%25" in line for line in lines)


def test_text_prints_bracketed_values_verbatim() -> None:
    """Rich read ``[bold]`` in a spec value as markup and swallowed it."""
    out = _run("agent@3", "agent@4").stdout
    assert "[bold]" in out


@pytest.mark.parametrize("fmt", ["text", "json", "github-annotation"])
def test_gate_exit_codes_are_the_same_in_every_format(fmt: str) -> None:
    differ = _run("agent@1", "agent@2", "--output-format", fmt, "--assert-no-regressions")
    assert differ.exit_code == 1, differ.output
    same = _run("agent@1", "agent@1", "--output-format", fmt, "--assert-no-regressions")
    assert same.exit_code == 0, same.output
    assert _run("agent@1", "agent@2", "--output-format", fmt).exit_code == 0
