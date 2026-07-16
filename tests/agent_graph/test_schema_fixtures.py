"""Golden-fixture schema tests: every fixture behaves as its filename asserts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "agent-execution-graph"
)
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "agent-execution-graph.schema.json"
)

VALID = sorted(FIXTURES.glob("valid-*.json"))
INVALID = sorted(FIXTURES.glob("invalid-*.json"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_fixture_inventory_is_complete() -> None:
    assert len(VALID) == 4
    assert len(INVALID) == 7


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixtures_pass(validator: Draft202012Validator, path: Path) -> None:
    validator.validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixtures_are_rejected(
    validator: Draft202012Validator, path: Path
) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert not validator.is_valid(document), f"{path.name} unexpectedly validated"
