"""Golden fixtures (design/spec) vs. the graduated /schemas/ copies (ADR-0135).

The three masking schemas graduated from ``design/spec/schemas/`` to
``/schemas/`` on acceptance. Every golden fixture must behave as its
filename asserts against the *graduated* copies — proving the graduation
changed identity metadata only, never the contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

_ROOT = Path(__file__).parents[2]
_SCHEMAS = _ROOT / "schemas"
_FIXTURES = _ROOT / "design" / "spec" / "fixtures" / "pii-masking-pipeline"

_SCHEMA_BY_PREFIX = {
    "config": "masking-config.schema.json",
    "finding": "masker-finding.schema.json",
    "error": "masker-error.schema.json",
}

_FIXTURE_FILES = sorted(_FIXTURES.glob("*.json"))


def _validator(prefix: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((_SCHEMAS / _SCHEMA_BY_PREFIX[prefix]).read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


def test_all_fourteen_fixtures_present() -> None:
    assert len(_FIXTURE_FILES) == 14


@pytest.mark.parametrize("fixture", _FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_behaves_as_filename_asserts(fixture: Path) -> None:
    prefix = fixture.stem.split("-", 1)[0]
    validator = _validator(prefix)
    data = json.loads(fixture.read_text())
    errors = list(validator.iter_errors(data))
    if "-valid" in fixture.stem and "-invalid" not in fixture.stem:
        assert errors == [], f"{fixture.name} must validate: {errors}"
    else:
        assert errors, f"{fixture.name} must be rejected"


@pytest.mark.parametrize(
    "fixture",
    [p for p in _FIXTURE_FILES if p.stem.startswith("config-valid")],
    ids=lambda p: p.stem,
)
def test_valid_config_fixtures_also_pass_the_runtime_loader(
    fixture: Path, tmp_path: Path
) -> None:
    """The Pydantic loader accepts exactly what the JSON schema accepts."""
    import yaml

    from novafabric.masking import load_masking_config

    as_yaml = tmp_path / "masking.yaml"
    as_yaml.write_text(yaml.dump(json.loads(fixture.read_text())))
    load_masking_config(as_yaml)  # must not raise
