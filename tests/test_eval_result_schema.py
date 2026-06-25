# tests/test_eval_result_schema.py
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path("schemas/eval-result.schema.json")
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def eval_result_schema() -> dict:  # type: ignore[type-arg]
    return json.loads(SCHEMA_PATH.read_text())


def test_valid_fixture_passes(eval_result_schema: dict) -> None:  # type: ignore[type-arg]
    data = json.loads((FIXTURES_DIR / "eval_result_valid.json").read_text())
    jsonschema.validate(data, eval_result_schema, format_checker=jsonschema.FormatChecker())


def test_invalid_fixture_rejected_missing_oci_digest(eval_result_schema: dict) -> None:  # type: ignore[type-arg]
    data = json.loads((FIXTURES_DIR / "eval_result_invalid.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, eval_result_schema, format_checker=jsonschema.FormatChecker())


def test_out_of_range_p_value_rejected(eval_result_schema: dict) -> None:  # type: ignore[type-arg]
    data = {
        "schema_version": "0.1.0",
        "suite_id": "gaia-level-1",
        "suite_version": "2024-06",
        "oci_digest": "sha256:a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4",
        "run_at": "2026-05-11T09:00:00.000Z",
        "capsule_id": "01HWXYZ1234ABCDEFGHIJKLMNO",
        "passed": True,
        "metrics": [
            {
                "name": "accuracy",
                "value": 0.72,
                "unit": "percent",
                "threshold": 0.70,
                "passed": True,
            }
        ],
        "statistical_context": {
            "p_value": 1.5,
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, eval_result_schema, format_checker=jsonschema.FormatChecker())
