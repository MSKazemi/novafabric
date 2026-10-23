"""L25: a malformed custom audit profile is a named validation error, not a traceback.

``compliance/audit/loader.py`` indexed the YAML with ``data["framework"]`` and
``ctrl_data["title"]``; a missing key raised a bare ``KeyError`` that ``nova audit``
(which catches only ``ValueError``) let escape as a traceback, and ``nova serve`` turned
into a 500. The loader now raises :class:`ProfileValidationError` (a ``ValueError``)
naming the file and the missing field; ``nova audit`` exits 2 for it. An unknown
profile id still exits 1, as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.audit.loader import (
    BUILT_IN_PROFILES,
    ProfileValidationError,
    load_profile,
)

runner = CliRunner()

_GOOD = """\
id: custom
name: Custom
framework: In-house
version: 1
controls:
  - id: C-1
    title: Calls are recorded
    description: Every model call is captured.
    evidence_requirements:
      - evidence_type: model_calls
        description: model-calls.jsonl present
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(text)
    return p


def test_a_well_formed_custom_profile_loads(tmp_path: Path) -> None:
    profile = load_profile(str(_write(tmp_path, _GOOD)))
    assert profile.id == "custom" and profile.controls[0].framework == "In-house"


@pytest.mark.parametrize("profile_id", BUILT_IN_PROFILES)
def test_every_built_in_profile_still_loads(profile_id: str) -> None:
    assert load_profile(profile_id).controls


@pytest.mark.parametrize(
    ("text", "names"),
    [
        (_GOOD.replace("framework: In-house\n", ""), "framework"),
        (_GOOD.replace("    title: Calls are recorded\n", ""), "title"),
        (_GOOD.replace("        description: model-calls.jsonl present\n", ""), "description"),
        ("- just\n- a list\n", "mapping"),
        (_GOOD.replace("controls:\n", "controls: nope\nignored:\n"), "controls"),
        ("id: [unclosed\n", "YAML"),
        (_GOOD.replace("    title: Calls are recorded\n", "    title: x\n    weight: heavy\n"), "weight"),
        ("", "mapping"),
    ],
)
def test_malformed_profile_raises_a_named_error(tmp_path: Path, text: str, names: str) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(ProfileValidationError) as info:
        load_profile(str(path))
    message = str(info.value)
    assert names in message
    assert str(path) in message
    assert isinstance(info.value, ValueError)  # every existing ValueError handler still catches it


def test_cli_exits_2_with_a_message_not_a_traceback(tmp_path: Path) -> None:
    path = _write(tmp_path, _GOOD.replace("framework: In-house\n", ""))
    result = runner.invoke(
        app, ["audit", "map", "--profile", str(path), "--data-dir", str(tmp_path)],
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "framework" in result.output
    assert "Traceback" not in result.output


def test_cli_unknown_profile_id_still_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["audit", "map", "--profile", "no-such-profile", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
